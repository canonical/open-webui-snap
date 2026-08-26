#!/usr/bin/env python3
"""Seed bundled Open WebUI plugins (functions) on install/refresh.

Plugins live only in the ``function`` table of ``webui.db``, so this script
seeds them via Open WebUI's Python model API.  It runs on every start of the
snap (install and refresh) and unconditionally writes the bundled version of
each plugin into the database:

  * a missing row is inserted and activated, so the plugin comes back if the
    database was reset, restored from a backup, or the function was deleted;
  * an existing row has its name/content/metadata overwritten with the bundled
    version, while the user's ``is_active``/``is_global`` toggles are preserved.

Disabling (rather than deleting) a function is therefore the supported way to
opt out of a bundled plugin: the toggle survives, a deletion does not.

Because the database is only safe to touch once Open WebUI has created it and
finished its migrations, the script first waits for the server's health
endpoint (migrations run during startup, before the server serves requests).
"""

import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

SNAP = os.environ.get("SNAP", "")
SNAP_COMMON = os.environ.get("SNAP_COMMON", "")
SNAP_DATA = os.environ.get("SNAP_DATA", "")

PLUGINS_DIR = os.path.join(SNAP, "plugins")
DATA_DIR = os.path.join(SNAP_COMMON, "data")
DB_PATH = os.path.join(DATA_DIR, "webui.db")
# The server (`open-webui serve`) generates/loads its signing secret from a
# ``.webui_secret_key`` file in its working directory ($SNAP_DATA) and exports it
# as WEBUI_SECRET_KEY.  Importing open_webui hard-requires that variable, so we
# load the same file here to stay in step with the running server.
SECRET_KEY_FILE = os.path.join(SNAP_DATA, ".webui_secret_key")

# How long to wait for the server to become ready (first launch creates and
# migrates the database; a refresh may run further migrations) before giving up
# (this script re-runs on the next snap start/refresh).
SERVER_READY_TIMEOUT = 600  # seconds
POLL_INTERVAL = 5
DEFAULT_PORT = "8080"
HEALTH_TIMEOUT = 5  # seconds per health probe
# The commit can transiently fail with "database is locked" while the server is
# writing; retry a few times before giving up.
COMMIT_RETRIES = 5
COMMIT_RETRY_DELAY = 3


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

def server_port() -> str:
    """Return the port the server is configured to listen on."""
    try:
        port = subprocess.run(
            ["snapctl", "get", "port"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return DEFAULT_PORT
    return port or DEFAULT_PORT


def server_healthy(port: str) -> bool:
    """Return True once Open WebUI answers on /health.

    Open WebUI runs its database migrations during startup, before it starts
    serving, so a healthy server means the schema is settled and it is safe to
    write to the ``function`` table.  The server binds the configured host, but
    loopback always works from inside the snap.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


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


def wait_for_database() -> bool:
    """Block until the server is healthy and the ``function`` table exists."""
    port = server_port()
    deadline = time.monotonic() + SERVER_READY_TIMEOUT
    while time.monotonic() < deadline:
        if server_healthy(port) and function_table_exists():
            return True
        time.sleep(POLL_INTERVAL)
    return server_healthy(port) and function_table_exists()


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------

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


def seed() -> bool:
    """Seed all bundled plugins.

    Returns True when nothing retryable failed. A False return means the caller
    should exit non-zero so the (oneshot) service is restarted and tries again.
    """
    plugins = discover_plugins()
    if not plugins:
        return True

    # Bind to the same signing secret as the running server before importing
    # open_webui, which hard-requires WEBUI_SECRET_KEY.
    if not ensure_secret_key():
        print(
            f"  WEBUI_SECRET_KEY not set and {SECRET_KEY_FILE} not readable yet; "
            "will retry."
        )
        return False

    import asyncio

    return asyncio.run(_seed_async(plugins))


async def _seed_async(plugins) -> bool:
    # Import Open WebUI's stable model API.  DATA_DIR is inherited from the
    # daemon environment so this binds to the same webui.db as the server.  The
    # API is async in current Open WebUI, so this runs inside an event loop.
    from open_webui.models.functions import Functions, FunctionForm, FunctionMeta
    from open_webui.models.users import Users
    from open_webui.utils.plugin import load_function_module_by_id, replace_imports

    super_admin = await Users.get_super_admin_user()
    owner_id = super_admin.id if super_admin else ""

    ok = True
    for function_id, path, content in plugins:
        print(f"Seeding plugin '{function_id}' from {path}")
        content = replace_imports(content)

        # Validate the plugin and determine its type via Open WebUI's own loader.
        try:
            _module, function_type, frontmatter = await load_function_module_by_id(
                function_id, content=content
            )
        except Exception as exc:  # noqa: BLE001 - report and skip a bad plugin
            # A plugin that cannot be loaded is broken, not transient: retrying
            # would never help, so do not mark the run as retryable.
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
            print(f"  ERROR: failed to seed plugin '{function_id}', will retry.")
            ok = False
            continue

        print(f"  Plugin '{function_id}' seeded ({function_type}).")

    return ok


async def _upsert(Functions, FunctionForm, function_id, function_type, name, content, meta, owner_id):
    """Insert a new function or overwrite the content of an existing one.

    Retries around the commit to tolerate transient SQLite locking while the
    server is writing.  Open WebUI's insert API cannot set ``is_active``, so a
    new row has to be activated in a second step; if that step fails the row is
    deleted again so the next attempt starts from a clean state instead of
    leaving a dormant function that is indistinguishable from one the user
    disabled on purpose.
    """
    import asyncio

    for attempt in range(1, COMMIT_RETRIES + 1):
        existing = await Functions.get_function_by_id(function_id)
        if existing is None:
            form = FunctionForm(id=function_id, name=name, content=content, meta=meta)
            inserted = await Functions.insert_new_function(owner_id, function_type, form)

            if inserted is not None:
                # Newly seeded plugins are active by default so their models
                # show up immediately.
                if await Functions.update_function_by_id(function_id, {"is_active": True}) is not None:
                    return True
                # Activation failed: undo the insert so this does not look like
                # a user-disabled function on the next attempt.
                await Functions.delete_function_by_id(function_id)
        else:
            # Overwrite the content/metadata with the bundled version but
            # preserve the user's active/global toggles.
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
    if not wait_for_database():
        print(
            "Open WebUI did not become healthy with an initialised database "
            "within timeout; exiting non-zero so the service is restarted and "
            "seeding is retried."
        )
        sys.exit(1)
    try:
        ok = seed()
    except Exception as exc:  # noqa: BLE001
        # Never abort the snap install/refresh with a traceback, but do exit
        # non-zero: the service is restarted on failure so a transient import,
        # lock or initialisation error is retried instead of silently dropping
        # the bundled plugin until the next refresh.
        print(f"Plugin seeding failed (will retry): {exc}")
        sys.exit(1)
    if not ok:
        print("Plugin seeding incomplete (will retry).")
        sys.exit(1)
    print("Plugin seeding complete.")


if __name__ == "__main__":
    main()
