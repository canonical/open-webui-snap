#!/usr/bin/python3

import json
import os
import sqlite3

print("Syncing configs")

DB_PATH = os.path.join(os.environ.get("SNAP_COMMON", ""), "data", "webui.db")


def has_snap_tag(api_config: dict) -> bool:
    """Return True if the api_config entry has a tag object with name 'snap'."""
    tags = api_config.get("tags", [])
    return any(tag.get("name") == "snap" for tag in tags if isinstance(tag, dict))


def remove_snap_entries(section: dict) -> dict:
    """
    Remove paired base_urls / api_configs entries where the api_configs
    entry contains a tag named 'snap'.  Works for both the 'openai' and
    'ollama' sections.
    """
    base_urls = section.get("base_urls") or section.get("api_base_urls", [])
    api_configs = section.get("api_configs", {})

    # Determine which indices to keep
    keep_indices = []
    for i in range(len(base_urls)):
        cfg = api_configs.get(str(i), {})
        if not has_snap_tag(cfg):
            keep_indices.append(i)

    # Rebuild the base_urls list
    new_base_urls = [base_urls[i] for i in keep_indices]

    # Rebuild the api_keys list if present (openai section)
    new_section = dict(section)
    if "api_base_urls" in section:
        new_section["api_base_urls"] = new_base_urls
        api_keys = section.get("api_keys", [])
        new_section["api_keys"] = [api_keys[i] for i in keep_indices if i < len(api_keys)]
    else:
        new_section["base_urls"] = new_base_urls

    # Rebuild api_configs with re-numbered keys
    new_api_configs = {}
    for new_idx, old_idx in enumerate(keep_indices):
        new_api_configs[str(new_idx)] = api_configs.get(str(old_idx), {})
    new_section["api_configs"] = new_api_configs

    return new_section


def sync_configs():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}, skipping.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, data FROM config LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            print("No row found in config table, skipping.")
            return

        row_id, data_json = row
        config = json.loads(data_json)

        # Process openai section
        if "openai" in config:
            config["openai"] = remove_snap_entries(config["openai"])

        # Process ollama section
        if "ollama" in config:
            config["ollama"] = remove_snap_entries(config["ollama"])

        updated_json = json.dumps(config)
        cursor.execute("UPDATE config SET data = ? WHERE id = ?", (updated_json, row_id))
        conn.commit()
        print("Config updated successfully.")
    finally:
        conn.close()


sync_configs()
