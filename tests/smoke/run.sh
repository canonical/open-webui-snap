#!/bin/bash
# Run the smoke test suite directly on the host machine.
#
# Installs the target build and runs tests/smoke/, which ends by disconnecting
# the gemma4 interface and asserting the model disappears.
#
# The upgrade scenario lives in a separate runner: tests/upgrade/run.sh.
#
# Configuration (environment variables):
#   OWUI_SNAP=path/to/open-webui.snap   install a local build, OR
#   OWUI_CHANNEL=latest/edge/pr-123     install from a store channel
#   GEMMA4_CHANNEL=stable               (optional) gemma4 channel
#   OWUI_CLEANUP=true                   (optional) remove snaps on exit
#
# Example:
#   OWUI_CHANNEL=latest/edge ./tests/smoke/run.sh
#
# Requirements: snapd, Python 3, a venv at .venv/ (or requirements installed globally).

set -euo pipefail

# Abort the whole script on Ctrl+C / termination instead of letting bash
# continue to the next command after pytest handles the interrupt itself.
# Exit 130 still fires the EXIT trap (cleanup) when OWUI_CLEANUP is set.
trap 'echo ""; echo "=== Interrupted, aborting ==="; exit 130' INT TERM

source "$(dirname "$0")/../shared/helpers.sh"

resolve_config

activate_venv

if cleanup_enabled; then
  trap cleanup EXIT
fi

# Remove existing installations so we start clean.
cleanup

install_owui_target
install_gemma4
connect_interface

wait_for_server_stable

install_test_deps

cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Run smoke tests against the target build
# ---------------------------------------------------------------------------
echo "=== Running smoke tests ==="
set +e
pytest tests/smoke/ -v
EXIT_CODE=$?
set -e

if [[ $EXIT_CODE -ne 0 ]]; then
  dump_logs
fi

exit $EXIT_CODE
