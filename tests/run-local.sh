#!/usr/bin/env bash
# run-local.sh — Run the Open WebUI snap integration tests on the host machine.
#
# Python dependencies are installed into an isolated virtualenv under
# tests/.venv and never installed globally.  Snap operations still require
# sudo, but nothing else touches the host system.
#
# Usage:
#   ./tests/run-local.sh [--snap-file <path>] [--channel <channel>]
#                        [--skip-snap-install] [--keep-snaps] [pytest-args…]
#
# Options:
#   --snap-file <path>    Install open-webui from a locally built .snap file.
#   --channel <channel>   Snap Store channel to install from (default: latest/edge).
#   --skip-snap-install   Skip snap install/connect steps (snaps already running).
#   --keep-snaps          Do not remove snaps after the test run.
#   Any remaining arguments are forwarded to pytest.
#
# Requirements (host):
#   - snapd running  (sudo apt install snapd)
#   - sudo privileges (for snap operations only)
#   - python3 with venv support  (sudo apt install python3-venv)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

SNAP_FILE=""
SNAP_CHANNEL="latest/edge"
SKIP_SNAP_INSTALL=false
KEEP_SNAPS=false
PYTEST_ARGS=()

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snap-file)          SNAP_FILE="$(realpath "$2")"; shift 2 ;;
    --channel)            SNAP_CHANNEL="$2"; shift 2 ;;
    --skip-snap-install)  SKIP_SNAP_INSTALL=true; shift ;;
    --keep-snaps)         KEEP_SNAPS=true; shift ;;
    *)                    PYTEST_ARGS+=("$1"); shift ;;
  esac
done

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}══ $* ${NC}"; }

# ── Preflight checks ──────────────────────────────────────────────────────────
command -v snap    >/dev/null 2>&1 || error "snapd not found.  sudo apt install snapd"
command -v python3 >/dev/null 2>&1 || error "python3 not found."
python3 -c "import venv" 2>/dev/null || error "python3-venv not found.  sudo apt install python3-venv"
command -v curl    >/dev/null 2>&1 || error "curl not found.  sudo apt install curl"

# ── Cleanup trap ──────────────────────────────────────────────────────────────
cleanup() {
  local exit_code=$?
  if [[ "$KEEP_SNAPS" == false && "$SKIP_SNAP_INSTALL" == false ]]; then
    step "Removing snaps"
    sudo snap remove open-webui --purge 2>/dev/null || true
    sudo snap remove gemma4     --purge 2>/dev/null || true
  else
    info "Leaving snaps installed (--keep-snaps or --skip-snap-install was set)."
  fi
  exit $exit_code
}
trap cleanup EXIT

# ── Virtualenv setup ──────────────────────────────────────────────────────────
step "Setting up Python virtualenv"
if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# Activate venv — all pip/pytest/playwright calls below use it automatically
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

info "Installing Python test dependencies into virtualenv..."
pip install -q --upgrade pip
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# Use the system-installed Chromium rather than Playwright's bundled build.
# Playwright's bundled Chromium only supports specific Ubuntu versions; using
# the system browser avoids that constraint entirely and keeps the venv clean.
find_chromium() {
  for candidate in \
    "$(which chromium-browser 2>/dev/null)" \
    "$(which chromium        2>/dev/null)" \
    "$(which google-chrome   2>/dev/null)" \
    /snap/chromium/current/usr/lib/chromium-browser/chromium-browser \
    /usr/bin/chromium-browser \
    /usr/bin/chromium; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return; }
  done
}

CHROMIUM_PATH="$(find_chromium)"
if [[ -z "$CHROMIUM_PATH" ]]; then
  error "No system Chromium found. Install it with: sudo snap install chromium"
fi
info "Using Chromium at: $CHROMIUM_PATH"

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="$CHROMIUM_PATH"

# ── Snap install ──────────────────────────────────────────────────────────────
if [[ "$SKIP_SNAP_INSTALL" == false ]]; then
  step "Installing snaps"

  info "Installing gemma4..."
  sudo snap install gemma4

  info "Waiting for gemma4 services to become active..."
  timeout 120 bash -c \
    'until sudo snap services gemma4 | grep -E "^gemma4\." | grep -v inactive; do sleep 3; done' \
    || warn "gemma4 services did not become active within 120 s — continuing."

  if [[ -n "$SNAP_FILE" ]]; then
    info "Installing open-webui from local snap file: $SNAP_FILE"
    sudo snap install --dangerous "$SNAP_FILE"
  else
    info "Installing open-webui from channel: $SNAP_CHANNEL"
    sudo snap install open-webui --channel "$SNAP_CHANNEL"
  fi

  info "Connecting content interface..."
  sudo snap connect open-webui:config gemma4:open-webui
else
  info "--skip-snap-install: assuming snaps are already installed and connected."
fi

# ── Wait for server ───────────────────────────────────────────────────────────
step "Waiting for open-webui server"
timeout 600 bash -c \
  'until curl -sf http://localhost:8080/health > /dev/null; do echo "  ...waiting"; sleep 5; done' \
  || error "Server did not become healthy within 10 min.  Check: sudo snap logs open-webui.server"
info "Server is up."

# ── Check startup logs ────────────────────────────────────────────────────────
step "Checking server logs for startup errors"
LOGS=$(sudo snap logs open-webui.server -n 150)
echo "$LOGS"
if echo "$LOGS" | grep -iE "Traceback \(most recent|CRITICAL|Failed to start"; then
  warn "Possible startup errors detected above — proceeding with tests."
fi

# ── Run tests ─────────────────────────────────────────────────────────────────
step "Running integration tests"
cd "$REPO_ROOT"

export BASE_URL="${BASE_URL:-http://localhost:8080}"
export ADMIN_NAME="${ADMIN_NAME:-Test Admin}"
export ADMIN_EMAIL="${ADMIN_EMAIL:-testadmin@example.com}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-TestPassword123!}"

if [[ ${#PYTEST_ARGS[@]} -eq 0 ]]; then
  pytest tests/ -v \
    --screenshot=only-on-failure \
    --video=retain-on-failure \
    --tracing=retain-on-failure \
    --output=test-results
else
  pytest "${PYTEST_ARGS[@]}"
fi
