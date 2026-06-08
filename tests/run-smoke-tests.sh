#!/bin/bash
# Run the smoke test suite directly on the host machine.
#
# open-webui can be installed from a local snap file or from the snap store.
# gemma4 is installed from the store (snapd caches it, so subsequent runs skip
# the large download).
#
# Usage:
#   ./tests/run-smoke-tests.sh --snap path/to/open-webui.snap [--gemma4-channel stable]
#   ./tests/run-smoke-tests.sh --channel latest/edge/pr-123   [--gemma4-channel stable]
#
# Requirements: snapd, Python 3, a venv at .venv/ (or requirements installed globally).

set -euo pipefail

SNAP_FILE=""
SNAP_CHANNEL=""
GEMMA4_CHANNEL="stable"
DO_CLEANUP=false

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
  echo "Usage: $0 (--snap <file> | --channel <channel>) [--gemma4-channel <channel>] [--cleanup]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --snap)            SNAP_FILE="$2";         shift 2 ;;
    --channel)         SNAP_CHANNEL="$2";      shift 2 ;;
    --gemma4-channel)  GEMMA4_CHANNEL="$2";    shift 2 ;;
    --cleanup)         DO_CLEANUP=true;         shift ;;
    *)                 usage ;;
  esac
done

if [[ -z "$SNAP_FILE" && -z "$SNAP_CHANNEL" ]]; then
  usage
fi
if [[ -n "$SNAP_FILE" && -n "$SNAP_CHANNEL" ]]; then
  echo "Error: --snap and --channel are mutually exclusive." >&2
  exit 1
fi

[[ -n "$SNAP_FILE" ]] && SNAP_FILE="$(realpath "$SNAP_FILE")"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ---------------------------------------------------------------------------
# Resolve Python / pytest (prefer repo venv, fall back to system)
# ---------------------------------------------------------------------------
if [[ ! -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  python3 -m venv "${REPO_ROOT}/.venv"
fi
source "${REPO_ROOT}/.venv/bin/activate"

# ---------------------------------------------------------------------------
# Cleanup: remove snaps we installed (restores host to prior state)
# ---------------------------------------------------------------------------
cleanup() {
  echo ""
  echo "=== Removing open-webui ==="
  sudo snap remove open-webui 2>/dev/null || true
  echo "=== Removing gemma4 ==="
  sudo snap remove gemma4 2>/dev/null || true
}

if [[ "$DO_CLEANUP" == true ]]; then
  trap cleanup EXIT
fi

# ---------------------------------------------------------------------------
# Remove existing installations so we start clean
# ---------------------------------------------------------------------------
cleanup

# ---------------------------------------------------------------------------
# Install snaps
# ---------------------------------------------------------------------------
if [[ -n "$SNAP_FILE" ]]; then
  echo "=== Installing open-webui from $(basename "$SNAP_FILE") ==="
  sudo snap install --dangerous "$SNAP_FILE"
else
  echo "=== Installing open-webui from channel $SNAP_CHANNEL ==="
  sudo snap install open-webui --channel="$SNAP_CHANNEL"
fi

echo "=== Installing gemma4 from channel $GEMMA4_CHANNEL ==="
sudo snap install gemma4 --channel="$GEMMA4_CHANNEL"

echo "=== Connecting snap interface ==="
sudo snap connect open-webui:config gemma4:open-webui

# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------
echo "=== Installing test dependencies ==="
python3 -m pip install --quiet -r "$REPO_ROOT/tests/smoke/requirements.txt"

echo "=== Running smoke tests ==="
cd "$REPO_ROOT"
pytest tests/smoke/ -v
EXIT_CODE=$?

# ---------------------------------------------------------------------------
# Dump logs on failure
# ---------------------------------------------------------------------------
if [[ $EXIT_CODE -ne 0 ]]; then
  echo ""
  echo "=== journalctl snap.open-webui.server ==="
  journalctl -u snap.open-webui.server --no-pager -n 500 || true
  echo ""
  echo "=== snap logs open-webui ==="
  snap logs open-webui -n 100 || true
fi

exit $EXIT_CODE
