# Open WebUI snap tests

Two suites exercise the `open-webui` snap against the `gemma4` model snap:

| Suite     | Runner                | What it does                                                                 |
|-----------|-----------------------|-----------------------------------------------------------------------------|
| `smoke`   | `tests/smoke/run.sh`  | Installs the target build, runs the smoke tests, and checks the model disappears when the interface is disconnected. |
| `upgrade` | `tests/upgrade/run.sh`| Configures Gemma4 through the baseline API, upgrades to the target, checks the model survives, then replaces the temporary connection with a provider-interface import. |

Shared Python helpers live in `tests/shared/owui.py`; shared bash helpers in
`tests/shared/helpers.sh`.

Chat completion requests omit `chat_id` to use stateless HTTP inference.
Open WebUI 0.13.3 requires temporary (`local:` or `temporary:`) chats to have an
authenticated, live WebSocket session, which these API-only suites do not create.

The PDF RAG test asks for the license name and version in one short sentence,
with a 600-second completion read timeout and no explicit output-token limit.
PDF indexing retains its separate 120-second deadline; other inference requests
retain their 300-second read timeout.

Both suites use `open-webui:provider` connected to `gemma4:provider` (content ID
`inference-provider`). Gemma4 publishes
`$SNAP_COMMON/share/provider/provider.env`; Open WebUI imports its
`OPENAI_BASE_URL`. Ollama discovery is currently an empty stub, not a second
endpoint inferred from that URL.

The upgrade suite reads Gemma4's published URL and configures the baseline
through `/openai/config/update`, so the baseline may still use the old JSON
interface. After upgrading, it removes that temporary configuration and
connects the new interface, checking the imported endpoint has the `snap` tag.

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
`pytest` for its suite. On failure it dumps journal logs for all Open WebUI and
Gemma4 services since the runner started, including installation and setup,
without a line limit. The diagnostics also include memory and swap usage,
service memory/OOM status, and kernel OOM messages from the same time range.

> **Note:** the runners call `sudo snap ...`, so a passwordless `sudo` (or an
> interactive session) is required.
