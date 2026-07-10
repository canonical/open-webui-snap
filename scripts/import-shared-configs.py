#!/usr/bin/env python3

import json
import os
import sqlite3
import subprocess
import sys
import time

DB_PATH = os.path.join(os.environ.get("SNAP_COMMON", ""), "data", "webui.db")
SHARED_CONFIGS_DIR = os.path.join(os.environ.get("SNAP", ""), "shared-configs")

# Marker file used to remember that the server still needs to be restarted.
# snapctl refuses to restart while another snap change (e.g. an interface
# connect/disconnect) is in progress, so a restart may fail even though the
# database has already been updated.  In that case we drop this marker and a
# later invocation retries the restart even though the configs are in sync.
RESTART_MARKER = os.path.join(
    os.environ.get("SNAP_COMMON", ""), ".restart-pending"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_snap_tag(tags: list) -> list:
    """Return a copy of *tags* that always contains {"name": "snap"}."""
    tags = list(tags)
    if not any(isinstance(t, dict) and t.get("name") == "snap" for t in tags):
        tags.append({"name": "snap"})
    return tags


def has_snap_tag(api_config: dict) -> bool:
    """Return True if the api_config entry has a tag object with name 'snap'."""
    tags = api_config.get("tags", [])
    return any(isinstance(t, dict) and t.get("name") == "snap" for t in tags)


# ---------------------------------------------------------------------------
# Read snap-tagged entries from database
# ---------------------------------------------------------------------------

def read_snap_entries_from_db(config: dict) -> tuple[list[str], list[str]]:
    """
    Extract base_urls of all snap-tagged entries from the openai and ollama
    sections of the config dict.
    Returns (openai_urls, ollama_urls).
    """
    openai_urls: list[str] = []
    ollama_urls: list[str] = []

    openai = config.get("openai", {})
    api_base_urls = openai.get("api_base_urls", [])
    api_configs = openai.get("api_configs", {})
    for i, url in enumerate(api_base_urls):
        cfg = api_configs.get(str(i), {})
        if has_snap_tag(cfg):
            openai_urls.append(url)

    ollama = config.get("ollama", {})
    base_urls = ollama.get("base_urls", [])
    ollama_api_configs = ollama.get("api_configs", {})
    for i, url in enumerate(base_urls):
        cfg = ollama_api_configs.get(str(i), {})
        if has_snap_tag(cfg):
            ollama_urls.append(url)

    return openai_urls, ollama_urls


# ---------------------------------------------------------------------------
# Read shared configs from files
# ---------------------------------------------------------------------------

def read_shared_configs() -> tuple[list[dict], list[dict]]:
    """
    Walk $SNAP/shared-configs/<name>/ and collect every openai.json /
    ollama.json found there.  Returns (openai_cfgs, ollama_cfgs).
    """
    openai_cfgs: list[dict] = []
    ollama_cfgs: list[dict] = []

    if not os.path.isdir(SHARED_CONFIGS_DIR):
        print("No shared configurations directory found.")
        return openai_cfgs, ollama_cfgs

    for entry in sorted(os.listdir(SHARED_CONFIGS_DIR)):
        subdir = os.path.join(SHARED_CONFIGS_DIR, entry)
        if not os.path.isdir(subdir):
            continue

        for filename, target_list in (("openai.json", openai_cfgs), ("ollama.json", ollama_cfgs)):
            filepath = os.path.join(subdir, filename)
            if os.path.isfile(filepath):
                try:
                    with open(filepath) as fh:
                        cfg = json.load(fh)
                    print(f"  Loaded {filepath}")
                    target_list.append(cfg)
                except Exception as exc:
                    print(f"  WARNING: could not read {filepath}: {exc}")

    return openai_cfgs, ollama_cfgs


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

def remove_snap_entries(section: dict) -> dict:
    """
    Remove paired base_urls / api_configs entries where the api_configs
    entry contains a tag named 'snap'.  Works for both the 'openai' and
    'ollama' sections.
    """
    base_urls = section.get("base_urls") or section.get("api_base_urls", [])
    api_configs = section.get("api_configs", {})

    keep_indices = []
    for i in range(len(base_urls)):
        cfg = api_configs.get(str(i), {})
        if not has_snap_tag(cfg):
            keep_indices.append(i)

    new_base_urls = [base_urls[i] for i in keep_indices]

    new_section = dict(section)
    if "api_base_urls" in section:
        new_section["api_base_urls"] = new_base_urls
        api_keys = section.get("api_keys", [])
        new_section["api_keys"] = [api_keys[i] for i in keep_indices if i < len(api_keys)]
    else:
        new_section["base_urls"] = new_base_urls

    new_api_configs = {}
    for new_idx, old_idx in enumerate(keep_indices):
        new_api_configs[str(new_idx)] = api_configs.get(str(old_idx), {})
    new_section["api_configs"] = new_api_configs

    return new_section


# ---------------------------------------------------------------------------
# Addition
# ---------------------------------------------------------------------------

def add_openai_entry(section: dict, file_cfg: dict) -> dict:
    """Append a new OpenAI connection entry tagged with 'snap'."""
    new_section = dict(section)

    api_base_urls = list(section.get("api_base_urls", []))
    api_keys = list(section.get("api_keys", []))
    api_configs = dict(section.get("api_configs", {}))

    new_index = str(len(api_base_urls))

    api_base_urls.append(file_cfg["base_url"])
    api_keys.append("")
    api_configs[new_index] = {
        "enable": True,
        "tags": ensure_snap_tag([]),
        "prefix_id": "",
        "model_ids": [],
        "connection_type": "external",
        "auth_type": "none",
    }

    new_section["api_base_urls"] = api_base_urls
    new_section["api_keys"] = api_keys
    new_section["api_configs"] = api_configs

    return new_section


def add_ollama_entry(section: dict, file_cfg: dict) -> dict:
    """Append a new Ollama connection entry tagged with 'snap'."""
    new_section = dict(section)

    base_urls = list(section.get("base_urls", []))
    api_configs = dict(section.get("api_configs", {}))

    new_index = str(len(base_urls))

    base_urls.append(file_cfg["base_url"])
    api_configs[new_index] = {
        "enable": True,
        "tags": ensure_snap_tag([]),
        "prefix_id": "",
        "model_ids": [],
        "connection_type": "external",
        "auth_type": "none",
        "key": "",
    }

    new_section["base_urls"] = base_urls
    new_section["api_configs"] = api_configs

    return new_section


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Key/value config table access
# ---------------------------------------------------------------------------
#
# As of open-webui v0.10.x the `config` table is a key/value store:
#
#     CREATE TABLE config (
#         "key" TEXT NOT NULL,
#         value JSON NOT NULL,
#         updated_at BIGINT,
#         PRIMARY KEY ("key")
#     );
#
# Each setting is stored as its own row, e.g. `openai.api_base_urls`,
# `openai.api_keys`, `openai.api_configs`, `ollama.base_urls`,
# `ollama.api_configs`.  The values are JSON-encoded.

# Keys that make up the openai/ollama "sections" the rest of this script
# operates on.
OPENAI_KEYS = {
    "api_base_urls": "openai.api_base_urls",
    "api_keys": "openai.api_keys",
    "api_configs": "openai.api_configs",
}
OLLAMA_KEYS = {
    "base_urls": "ollama.base_urls",
    "api_configs": "ollama.api_configs",
}


def get_config_value(cursor, key, default):
    """Return the JSON-decoded value for *key*, or *default* if absent."""
    cursor.execute("SELECT value FROM config WHERE \"key\" = ?", (key,))
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return default
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return default
    # A stored JSON literal `null` decodes to None, which would break callers
    # that expect a list/dict (e.g. len(base_urls)).  Normalize it back to the
    # provided default.
    if value is None:
        return default
    return value


def set_config_value(cursor, key, value, updated_at):
    """Insert or update *key* with the JSON-encoded *value*."""
    payload = json.dumps(value)
    cursor.execute(
        "INSERT INTO config (\"key\", value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(\"key\") DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at",
        (key, payload, updated_at),
    )


def config_table_is_key_value(cursor) -> bool:
    """
    Return True only when the `config` table uses the new key/value schema
    (columns: "key", "value", ...).

    This guards against the upgrade scenario where a user moves from
    open-webui 0.9.x (which stored the whole config as a single JSON blob in
    a `data` column) to 0.10.x.  On first launch after such an upgrade the
    table may still be in the old schema until open-webui's own database
    migrations have run.  Writing to it before that point would either fail
    or corrupt the data that those migrations expect, so we simply skip and
    let open-webui migrate first; we run again on a later invocation.
    """
    try:
        cursor.execute("PRAGMA table_info(config)")
    except sqlite3.Error:
        return False

    columns = {row[1] for row in cursor.fetchall()}
    if not columns:
        # No config table at all.
        return False

    return "key" in columns and "value" in columns


def build_section(cursor, key_map):
    """Assemble a section dict from the individual config rows."""
    section = {}
    for field, key in key_map.items():
        default = {} if field.endswith("configs") else []
        section[field] = get_config_value(cursor, key, default)
    return section


def write_section(cursor, section, key_map, updated_at):
    """Write a section dict back to its individual config rows."""
    for field, key in key_map.items():
        if field in section:
            set_config_value(cursor, key, section[field], updated_at)


def _set_restart_pending():
    try:
        open(RESTART_MARKER, "w").close()
    except OSError as exc:
        print(f"Warning: could not write restart marker: {exc}")


def _clear_restart_pending():
    try:
        os.remove(RESTART_MARKER)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"Warning: could not remove restart marker: {exc}")


def _restart_pending():
    return os.path.exists(RESTART_MARKER)


def restart_server():
    """Restart the open-webui server daemon so it reloads its config.

    Only the ``server`` daemon is restarted (not the whole snap) so this
    oneshot service does not try to restart itself.  snapctl refuses to
    restart while another snap change is in progress; rather than crashing we
    record a pending-restart marker and let a later invocation retry.
    """
    print("Restarting open-webui server...")
    try:
        subprocess.run(["snapctl", "restart", "open-webui.server"], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Could not restart open-webui server ({exc}); "
              "leaving a pending-restart marker to retry on a later run.")
        _set_restart_pending()
        return False
    print("Server restarted.")
    _clear_restart_pending()
    return True


def check_and_sync():
    # Check if database exists
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}, exiting.")
        sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # Only proceed once open-webui's own migrations have converted the
        # config table to the new key/value schema.  If it is still in the
        # old 0.9.x `data`-blob form, bail out so we don't interfere with the
        # pending migrations; we will run again on a later invocation.
        if not config_table_is_key_value(cursor):
            print("Config table is not in the new key/value schema yet "
                  "(open-webui migrations may be pending), exiting.")
            sys.exit(0)

        # The config table is a key/value store.  Assemble the openai and
        # ollama "sections" from their individual rows so the rest of the
        # logic can keep operating on dicts.
        config = {
            "openai": build_section(cursor, OPENAI_KEYS),
            "ollama": build_section(cursor, OLLAMA_KEYS),
        }

        # Read snap-tagged entries from the database
        db_openai_urls, db_ollama_urls = read_snap_entries_from_db(config)
        print(f"DB snap openai urls: {db_openai_urls}")
        print(f"DB snap ollama urls: {db_ollama_urls}")

        # Read shared configs from files
        shared_openai_cfgs, shared_ollama_cfgs = read_shared_configs()
        shared_openai_urls = [c["base_url"] for c in shared_openai_cfgs]
        shared_ollama_urls = [c["base_url"] for c in shared_ollama_cfgs]
        print(f"Shared openai urls: {shared_openai_urls}")
        print(f"Shared ollama urls: {shared_ollama_urls}")

        # Compare; if identical there is nothing to write.  We may still owe a
        # restart from a previous run whose restart could not complete.
        if sorted(db_openai_urls) == sorted(shared_openai_urls) and \
                sorted(db_ollama_urls) == sorted(shared_ollama_urls):
            if _restart_pending():
                print("Configs are in sync, but a previous restart did not "
                      "complete; retrying restart.")
                needs_restart = True
            else:
                print("Configs are in sync, nothing to do.")
                needs_restart = False
        else:
            # Apply changes: remove all snap entries, then re-add from shared
            # configs.
            print("Changes detected, updating database...")

            config["openai"] = remove_snap_entries(config["openai"])
            config["ollama"] = remove_snap_entries(config["ollama"])

            for file_cfg in shared_openai_cfgs:
                config["openai"] = add_openai_entry(config["openai"], file_cfg)

            for file_cfg in shared_ollama_cfgs:
                config["ollama"] = add_ollama_entry(config["ollama"], file_cfg)

            now = int(time.time())
            write_section(cursor, config["openai"], OPENAI_KEYS, now)
            write_section(cursor, config["ollama"], OLLAMA_KEYS, now)
            conn.commit()
            print("Database updated successfully.")
            needs_restart = True

    finally:
        conn.close()

    if needs_restart:
        restart_server()


if __name__ == "__main__":
    print("Checking shared configurations")
    check_and_sync()
