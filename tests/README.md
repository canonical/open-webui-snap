# Open WebUI snap tests

Two suites exercise the `open-webui` snap against the `gemma4` model snap:

| Suite     | Runner                | What it does                                                                 |
|-----------|-----------------------|-----------------------------------------------------------------------------|
| `smoke`   | `tests/smoke/run.sh`  | Installs the target build, runs the smoke tests, and checks the model disappears when the interface is disconnected. |
| `upgrade` | `tests/upgrade/run.sh`| Installs the baseline channel (`OWUI_FROM_CHANNEL`), then refreshes (upgrades) to the target build and checks the model survives. |

Shared Python helpers live in `tests/shared/owui.py`; shared bash helpers in
`tests/shared/helpers.sh`.

## Configuration

Both runners are configured entirely through environment variables:

| Variable           | Required            | Description                                                        |
|--------------------|---------------------|--------------------------------------------------------------------|
| `OWUI_SNAP`        | one of these two    | Path to a local `open-webui` `.snap` to install (`--dangerous`).   |
| `OWUI_CHANNEL`     | one of these two    | Store channel to install/refresh **to**, e.g. `latest/edge`.       |
| `OWUI_FROM_CHANNEL`| no (default `stable`) | *Upgrade only.* Baseline store channel to install and upgrade **from**. |
| `GEMMA4_CHANNEL`   | no (default `stable`) | Store channel for the `gemma4` snap.                             |
| `OWUI_CLEANUP`     | no (default off)    | Set to `true` to remove the installed snaps when the run exits.    |

Set exactly one of `OWUI_SNAP` or `OWUI_CHANNEL` (the upgrade target).

## Running manually

From the repository root:

```bash
# Smoke test against a local snap build
OWUI_SNAP=./open-webui_*.snap ./tests/smoke/run.sh

# Smoke test against a store channel
OWUI_CHANNEL=latest/edge ./tests/smoke/run.sh

# Upgrade test: stable -> target build, cleaning up snaps afterwards
OWUI_CHANNEL=latest/edge OWUI_CLEANUP=true ./tests/upgrade/run.sh

# Upgrade test from a specific baseline channel to a local snap
OWUI_FROM_CHANNEL=latest/beta OWUI_SNAP=./open-webui_*.snap ./tests/upgrade/run.sh
```

Each runner installs the snaps, waits for the server to stabilise, provisions a
`.venv/` at the repo root, installs `tests/smoke/requirements.txt`, and runs
`pytest` for its suite. On failure it dumps recent journal and snap logs.

> **Note:** the runners call `sudo snap ...`, so a passwordless `sudo` (or an
> interactive session) is required.
