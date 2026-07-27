#!/bin/bash
# Run the upgrade test suite directly on the host machine.
#
# Purges any existing open-webui, installs the baseline build from a store
# channel, connects the gemma4 interface, then refreshes (upgrades) to the
# target build and runs tests/upgrade/ to confirm the model survives.
#
# The plain smoke scenario lives in a separate runner: tests/smoke/run.sh.
#
# Configuration (environment variables):
#   OWUI_FROM_CHANNEL=stable            (optional) baseline channel to upgrade FROM
#   OWUI_SNAP=path/to/open-webui.snap   upgrade TO a local build, OR
#   OWUI_CHANNEL=latest/edge/pr-123     upgrade TO a store channel
#   GEMMA4_CHANNEL=stable               (optional) gemma4 channel
#   OWUI_CLEANUP=true                   (optional) remove snaps on exit
#
# Example (upgrade from stable to latest/edge):
#   OWUI_FROM_CHANNEL=stable OWUI_CHANNEL=latest/edge ./tests/upgrade/run.sh
#
# Requirements: snapd, Python 3, a venv at .venv/ (or requirements installed globally).

set -euo pipefail

# Abort the whole script on Ctrl+C / termination instead of letting bash
# continue to the next command after pytest handles the interrupt itself.
# Exit 130 still fires the EXIT trap (cleanup) when OWUI_CLEANUP is set.
trap 'echo ""; echo "=== Interrupted, aborting ==="; exit 130' INT TERM

source "$(dirname "$0")/../shared/helpers.sh"

resolve_config

# Baseline channel to install before upgrading to the target (upgrade-only).
FROM_CHANNEL="${OWUI_FROM_CHANNEL:-stable}"

activate_venv

if cleanup_enabled; then
  trap cleanup EXIT
fi

# Remove existing installations so we start clean.
cleanup

# Install the baseline build, then upgrade to the target below.
install_gemma4

echo "=== Installing open-webui from channel $FROM_CHANNEL (baseline) ==="
sudo snap install open-webui --channel="$FROM_CHANNEL"

connect_interface

wait_for_server_stable

install_test_deps

# Tell the upgrade tests what to refresh to.
if [[ -n "$SNAP_FILE" ]]; then
  export OWUI_UPGRADE_SNAP="$SNAP_FILE"
  echo "=== Upgrade target: $(basename "$SNAP_FILE") ==="
else
  export OWUI_UPGRADE_CHANNEL="$SNAP_CHANNEL"
  echo "=== Upgrade target: channel $SNAP_CHANNEL ==="
fi

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Run upgrade tests
# ---------------------------------------------------------------------------
echo "=== Running upgrade tests ==="
set +e
pytest tests/upgrade/ -v
EXIT_CODE=$?
set -e

if [[ $EXIT_CODE -ne 0 ]]; then
  dump_logs
fi

exit $EXIT_CODE
