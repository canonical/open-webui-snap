#!/usr/bin/env python3
import builtins
import json
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.environ.get("SNAP_COMMON", ""), "data", "webui.db")
SHARED_CONFIGS_DIR = os.path.join(os.environ.get("SNAP", ""), "shared-configs")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Store the original print function
builtin_print = builtins.print
log_prefix = "[import config]"


def print(*args, **kwargs):
    """My custom print() function with a prefix."""
    # Create a list of all arguments
    all_args = (log_prefix,) + args

    # Call the original print function with the modified arguments
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


# ---------------------------------------------------------------------------
# Addition
# ---------------------------------------------------------------------------

def add_openai_entry(section: dict, file_cfg: dict) -> dict:
    """
    Append a new OpenAI connection entry to the openai section using the
    base_url from an openai.json shared-config file.
    All other fields use default values. The 'snap' tag is always added.
    """
    new_section = dict(section)

    api_base_urls = list(section.get("api_base_urls", []))
    api_keys = list(section.get("api_keys", []))
    api_configs = dict(section.get("api_configs", {}))

    new_index = str(len(api_base_urls))

    api_base_urls.append(file_cfg["base_url"])
    api_keys.append("")
    api_configs[new_index] = {
        "enable": True,
        "tags": ensure_snap_tag([]),  # adds the snap tag
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
    """
    Append a new Ollama connection entry to the ollama section using the
    base_url from an ollama.json shared-config file.
    All other fields use default values. The 'snap' tag is always added.
    """
    new_section = dict(section)

    base_urls = list(section.get("base_urls", []))
    api_configs = dict(section.get("api_configs", {}))

    new_index = str(len(base_urls))

    base_urls.append(file_cfg["base_url"])
    api_configs[new_index] = {
        "enable": True,
        "tags": ensure_snap_tag([]),  # adds the snap tag
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
# Shared-config discovery
# ---------------------------------------------------------------------------

def read_shared_configs() -> tuple[list[dict], list[dict]]:
    """
    Walk $SNAP/shared-configs/<name>/ and collect every openai.json /
    ollama.json found there.  Returns (openai_cfgs, ollama_cfgs).
    """
    openai_cfgs: list[dict] = []
    ollama_cfgs: list[dict] = []

    if not os.path.isdir(SHARED_CONFIGS_DIR):
        print(f"No shared configurations to import.")
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
# Main
# ---------------------------------------------------------------------------

def apply_configs():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}, skipping.")
        return

    openai_cfgs, ollama_cfgs = read_shared_configs()

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

        # Remove all previously snap-managed entries
        if "openai" in config:
            config["openai"] = remove_snap_entries(config["openai"])
        if "ollama" in config:
            config["ollama"] = remove_snap_entries(config["ollama"])

        # Add entries from shared-configs
        for file_cfg in openai_cfgs:
            config.setdefault("openai", {"api_base_urls": [], "api_keys": [], "api_configs": {}})
            config["openai"] = add_openai_entry(config["openai"], file_cfg)

        for file_cfg in ollama_cfgs:
            config.setdefault("ollama", {"base_urls": [], "api_configs": {}})
            config["ollama"] = add_ollama_entry(config["ollama"], file_cfg)

        updated_json = json.dumps(config)
        cursor.execute("UPDATE config SET data = ? WHERE id = ?", (updated_json, row_id))
        conn.commit()
        print("Database updated successfully.")
    finally:
        conn.close()


if __name__ == "__main__":
    print("Importing configurations from connected snaps")
    apply_configs()
