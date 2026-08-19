#!/usr/bin/env python3
"""Seed bundled Open WebUI plugins (functions) on install/refresh.

Open WebUI has no directory- or env-based plugin loader: plugins live only in
the ``function`` table of ``webui.db``.  Rather than manipulating that internal
schema with raw SQL, this script uses Open WebUI's own Python model API
(``open_webui.models.functions.Functions`` et al.), which is a stable
abstraction that upstream keeps in step with any schema changes.  A companion
build-time check (see the ``plugin-api-check`` part in snapcraft.yaml) fails the
snap build if that API ever disappears, so breakage is caught at build time
rather than silently at runtime.

Behaviour:
* Waits (read-only) until the server has created and migrated the ``function``
  table, then exits early if it never appears within the deadline.
* For every ``$SNAP/plugins/*.py`` it computes a content hash and skips work when
  a marker records that exact hash was already seeded.  This makes the script
  idempotent, propagates plugin updates on refresh, and – crucially – does not
  fight the user: if they delete or edit a seeded plugin it stays that way until
  a *new* bundled version ships.
"""

import hashlib
import os
import sqlite3
import sys
import time

SNAP = os.environ.get("SNAP", "")
SNAP_COMMON = os.environ.get("SNAP_COMMON", "")
SNAP_DATA = os.environ.get("SNAP_DATA", "")

PLUGINS_DIR = os.path.join(SNAP, "plugins")
DATA_DIR = os.path.join(SNAP_COMMON, "data")
DB_PATH = os.path.join(DATA_DIR, "webui.db")
MARKER_DIR = os.path.join(DATA_DIR, ".owui-seeded")
# The server (`open-webui serve`) generates/loads its signing secret from a
# ``.webui_secret_key`` file in its working directory ($SNAP_DATA) and exports it
# as WEBUI_SECRET_KEY.  Importing open_webui hard-requires that variable, so we
# load the same file here to stay in step with the running server.
SECRET_KEY_FILE = os.path.join(SNAP_DATA, ".webui_secret_key")

# How long to wait for the server to create and migrate the database on first
# launch before giving up (this script re-runs on the next snap start/refresh).
DB_READY_TIMEOUT = 600  # seconds
DB_POLL_INTERVAL = 5
# The commit can transiently fail with "database is locked" while the server is
# writing; retry a few times before giving up.
COMMIT_RETRIES = 5
COMMIT_RETRY_DELAY = 3


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def function_table_exists() -> bool:
    """Return True if webui.db exists and already has the ``function`` table.

    Uses a read-only connection so we never create the database or race the
    server's own migrations.
    """
    if not os.path.exists(DB_PATH):
        return False
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='function'"
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def wait_for_function_table() -> bool:
    deadline = time.monotonic() + DB_READY_TIMEOUT
    while time.monotonic() < deadline:
        if function_table_exists():
            return True
        time.sleep(DB_POLL_INTERVAL)
    return function_table_exists()


# ---------------------------------------------------------------------------
# Marker helpers (idempotency)
# ---------------------------------------------------------------------------

def read_marker(function_id: str):
    path = os.path.join(MARKER_DIR, function_id)
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def write_marker(function_id: str, content_hash: str) -> None:
    os.makedirs(MARKER_DIR, exist_ok=True)
    path = os.path.join(MARKER_DIR, function_id)
    with open(path, "w") as fh:
        fh.write(content_hash)


def function_id_from_filename(filename: str) -> str:
    """Derive a valid Open WebUI function id from a plugin filename."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    ident = "".join(ch if ch.isalnum() else "_" for ch in stem).lower()
    if ident and ident[0].isdigit():
        ident = f"_{ident}"
    return ident


def discover_plugins() -> list[tuple[str, str, str]]:
    """Return (function_id, path, content) for every bundled plugin file."""
    plugins = []
    if not os.path.isdir(PLUGINS_DIR):
        print(f"No plugins directory found at {PLUGINS_DIR}, nothing to seed.")
        return plugins
    for entry in sorted(os.listdir(PLUGINS_DIR)):
        if not entry.endswith(".py"):
            continue
        path = os.path.join(PLUGINS_DIR, entry)
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            content = fh.read()
        plugins.append((function_id_from_filename(entry), path, content))
    return plugins


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def ensure_secret_key() -> bool:
    """Load WEBUI_SECRET_KEY from the server's key file if not already set.

    Importing open_webui aborts when WEBUI_SECRET_KEY is unset and auth is
    enabled.  The server writes/loads this key from ``$SNAP_DATA/.webui_secret_key``
    at startup, so by the time the ``function`` table exists the file is present.
    """
    if os.environ.get("WEBUI_SECRET_KEY"):
        return True
    try:
        key = open(SECRET_KEY_FILE, "r").read()
    except OSError:
        return False
    if not key:
        return False
    os.environ["WEBUI_SECRET_KEY"] = key
    return True


def seed() -> None:
    plugins = discover_plugins()
    if not plugins:
        return

    # Skip anything already seeded at its current content hash *before* importing
    # the (heavy) Open WebUI machinery, so the common "nothing changed" path is
    # cheap.
    pending = []
    for function_id, path, content in plugins:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if read_marker(function_id) == content_hash:
            print(f"Plugin '{function_id}' already seeded (unchanged), skipping.")
            continue
        pending.append((function_id, path, content, content_hash))

    if not pending:
        print("All bundled plugins already seeded.")
        return

    # Bind to the same signing secret as the running server before importing
    # open_webui, which hard-requires WEBUI_SECRET_KEY.
    if not ensure_secret_key():
        print(
            f"  WEBUI_SECRET_KEY not set and {SECRET_KEY_FILE} not readable yet; "
            "exiting, will retry on next start."
        )
        return

    import asyncio

    asyncio.run(_seed_async(pending))


async def _seed_async(pending) -> None:
    # Import Open WebUI's stable model API.  DATA_DIR is inherited from the
    # daemon environment so this binds to the same webui.db as the server.  The
    # API is async in current Open WebUI, so this runs inside an event loop.
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta
    from open_webui.models.users import Users
    from open_webui.utils.plugin import load_function_module_by_id, replace_imports

    super_admin = await Users.get_super_admin_user()
    owner_id = super_admin.id if super_admin else ""

    for function_id, path, content, content_hash in pending:
        print(f"Seeding plugin '{function_id}' from {path}")
        content = replace_imports(content)

        # Validate the plugin and determine its type via Open WebUI's own loader.
        try:
            _module, function_type, frontmatter = await load_function_module_by_id(
                function_id, content=content
            )
        except Exception as exc:  # noqa: BLE001 - report and skip a bad plugin
            print(f"  ERROR: could not load plugin '{function_id}': {exc}")
            continue

        name = frontmatter.get("title") or function_id
        meta = FunctionMeta(
            description=frontmatter.get("description"),
            manifest=frontmatter,
        )

        if not await _upsert(
            Functions, FunctionForm, function_id, function_type, name, content, meta, owner_id
        ):
            print(f"  ERROR: failed to seed plugin '{function_id}', will retry next run.")
            continue

        write_marker(function_id, content_hash)
        print(f"  Plugin '{function_id}' seeded ({function_type}).")


async def _upsert(Functions, FunctionForm, function_id, function_type, name, content, meta, owner_id):
    """Insert a new function or update the content of an existing one.

    Retries around the commit to tolerate transient SQLite locking while the
    server is writing.
    """
    import asyncio

    for attempt in range(1, COMMIT_RETRIES + 1):
        existing = await Functions.get_function_by_id(function_id)
        if existing is None:
            form = FunctionForm(id=function_id, name=name, content=content, meta=meta)
            result = await Functions.insert_new_function(owner_id, function_type, form)
            if result is not None:
                # Newly bundled plugins are active by default so their models
                # show up immediately.
                await Functions.update_function_by_id(function_id, {"is_active": True})
                return True
        else:
            # Update the content/metadata on refresh but preserve the user's
            # active/global toggles.
            result = await Functions.update_function_by_id(
                function_id,
                {"name": name, "content": content, "meta": meta.model_dump()},
            )
            if result is not None:
                return True

        if attempt < COMMIT_RETRIES:
            print(f"  Upsert attempt {attempt} failed; retrying in {COMMIT_RETRY_DELAY}s...")
            await asyncio.sleep(COMMIT_RETRY_DELAY)
    return False


def main() -> None:
    print("Seeding bundled Open WebUI plugins...")
    if not wait_for_function_table():
        print(
            "Database/function table not ready within timeout "
            "(Open WebUI may still be initialising); exiting, will retry on next start."
        )
        sys.exit(0)
    try:
        seed()
    except Exception as exc:  # noqa: BLE001
        # A seeding failure must never abort the snap install/refresh (this is a
        # oneshot service gating startup); log loudly and let it retry next start.
        print(f"Plugin seeding failed (will retry on next start): {exc}")
        sys.exit(0)
    print("Plugin seeding complete.")


if __name__ == "__main__":
    main()
