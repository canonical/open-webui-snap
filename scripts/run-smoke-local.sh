#!/bin/bash
# Run the smoke test suite directly on the host machine.
#
# open-webui is installed from a local snap file; gemma4 is installed from the
# store (snapd caches it, so subsequent runs skip the large download).
#
# Usage:
#   ./scripts/run-smoke-local.sh --snap path/to/open-webui.snap [--gemma4-channel stable]
#
# Requirements: snapd, Python 3, a venv at .venv/ (or requirements installed globally).

set -euo pipefail

SNAP_FILE=""
GEMMA4_CHANNEL="stable"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
  echo "Usage: $0 --snap <file> [--gemma4-channel <channel>]" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --snap)            SNAP_FILE="$2";         shift 2 ;;
    --gemma4-channel)  GEMMA4_CHANNEL="$2";    shift 2 ;;
    *)                 usage ;;
  esac
done

[[ -z "$SNAP_FILE" ]] && usage
SNAP_FILE="$(realpath "$SNAP_FILE")"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${REPO_ROOT}/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
PYTEST="$(dirname "$PYTHON")/pytest"

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
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Remove existing installations so we start clean
# ---------------------------------------------------------------------------
echo "=== Removing existing open-webui (if installed) ==="
sudo snap remove open-webui 2>/dev/null || true

echo "=== Removing existing gemma4 (if installed) ==="
sudo snap remove gemma4 2>/dev/null || true

# ---------------------------------------------------------------------------
# Install snaps
# ---------------------------------------------------------------------------
echo "=== Installing open-webui from $(basename "$SNAP_FILE") ==="
sudo snap install --dangerous "$SNAP_FILE"

echo "=== Installing gemma4 from channel $GEMMA4_CHANNEL ==="
sudo snap install gemma4 --channel="$GEMMA4_CHANNEL"

echo "=== Connecting snap interface ==="
sudo snap connect open-webui:config gemma4:open-webui

# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------
echo "=== Installing test dependencies ==="
"$PYTHON" -m pip install --quiet -r "$REPO_ROOT/tests/smoke/requirements.txt"

echo "=== Running smoke tests ==="
cd "$REPO_ROOT"
"$PYTEST" tests/smoke/ -v
EXIT_CODE=$?

# ---------------------------------------------------------------------------
# Dump logs on failure (before cleanup trap fires)
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
