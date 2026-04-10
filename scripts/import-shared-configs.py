#!/usr/bin/env python3

"""
Prompt used to generate this script:

This script runs every minute. Look into imprt-shared-configs to see how we did things before. This time do the following:
1. Check if database exists. If not exit.
2. Check if config table has a single entry. If not exit.
3. Read all openai and ollama configs from the database, which are tagged with "snap".
4. Read all openai and ollama configs from shared configs.
5. Compare the lists from the database to the lists from the shared configs.
6. If they are the same, exit.
7. Apply any changes, both removals and additions, to the database, so that the configs in the database tagged with snap reflects the configs shared via files.
8. If any changes are made, restart the service with snapctl restart open-webui.
"""

import builtins
import json
import os
import sqlite3
import subprocess
import sys

DB_PATH = os.path.join(os.environ.get("SNAP_COMMON", ""), "data", "webui.db")
SHARED_CONFIGS_DIR = os.path.join(os.environ.get("SNAP", ""), "shared-configs")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
builtin_print = builtins.print
log_prefix = "[check config]"


def print(*args, **kwargs):
    """Custom print() function with a prefix."""
    all_args = (log_prefix,) + args
    builtin_print(*all_args, **kwargs)


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

def check_and_sync():
    # 1. Check if database exists
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}, exiting.")
        sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # 2. Check if config table has a single entry
        cursor.execute("SELECT COUNT(*) FROM config")
        count = cursor.fetchone()[0]
        if count != 1:
            print(f"Config table has {count} entries (expected 1), exiting.")
            sys.exit(0)

        cursor.execute("SELECT id, data FROM config LIMIT 1")
        row = cursor.fetchone()
        row_id, data_json = row
        config = json.loads(data_json)

        # 3. Read snap-tagged entries from the database
        db_openai_urls, db_ollama_urls = read_snap_entries_from_db(config)
        print(f"DB snap openai urls: {db_openai_urls}")
        print(f"DB snap ollama urls: {db_ollama_urls}")

        # 4. Read shared configs from files
        shared_openai_cfgs, shared_ollama_cfgs = read_shared_configs()
        shared_openai_urls = [c["base_url"] for c in shared_openai_cfgs]
        shared_ollama_urls = [c["base_url"] for c in shared_ollama_cfgs]
        print(f"Shared openai urls: {shared_openai_urls}")
        print(f"Shared ollama urls: {shared_ollama_urls}")

        # 5 & 6. Compare; exit early if identical (order-insensitive)
        if sorted(db_openai_urls) == sorted(shared_openai_urls) and \
                sorted(db_ollama_urls) == sorted(shared_ollama_urls):
            print("Configs are in sync, nothing to do.")
            sys.exit(0)

        # 7. Apply changes: remove all snap entries, then re-add from shared configs
        print("Changes detected, updating database...")

        if "openai" in config:
            config["openai"] = remove_snap_entries(config["openai"])
        if "ollama" in config:
            config["ollama"] = remove_snap_entries(config["ollama"])

        for file_cfg in shared_openai_cfgs:
            config.setdefault("openai", {"api_base_urls": [], "api_keys": [], "api_configs": {}})
            config["openai"] = add_openai_entry(config["openai"], file_cfg)

        for file_cfg in shared_ollama_cfgs:
            config.setdefault("ollama", {"base_urls": [], "api_configs": {}})
            config["ollama"] = add_ollama_entry(config["ollama"], file_cfg)

        updated_json = json.dumps(config)
        cursor.execute("UPDATE config SET data = ? WHERE id = ?", (updated_json, row_id))
        conn.commit()
        print("Database updated successfully.")

    finally:
        conn.close()

    # 8. Restart the service
    print("Restarting open-webui service...")
    subprocess.run(["snapctl", "restart", "open-webui"], check=True)
    print("Service restarted.")


if __name__ == "__main__":
    print("Checking shared configurations")
    check_and_sync()
