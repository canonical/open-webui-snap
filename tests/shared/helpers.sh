# Shared helpers for the smoke and upgrade test runners.
#
# open-webui can be installed from a local snap file or from the snap store.
# gemma4 is installed from the store (snapd caches it, so subsequent runs skip
# the large download).
#
# Configuration is read from environment variables (see resolve_config):
#   OWUI_SNAP       Path to a local open-webui .snap to install (--dangerous).
#   OWUI_CHANNEL    Store channel to install/refresh from (e.g. latest/edge).
#                   Set exactly one of OWUI_SNAP or OWUI_CHANNEL.
#   GEMMA4_CHANNEL  Store channel for gemma4 (default: stable).
#   OWUI_CLEANUP    Remove installed snaps on exit when truthy (default: off).
#   OWUI_URL        Base URL of the server (default: http://localhost:8080).
#
# Source this from a runner script, e.g.:
#   source "$(dirname "$0")/../shared/helpers.sh"
#   resolve_config
#
# Requirements: snapd, Python 3, a venv at .venv/ (or requirements installed
# globally).

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OWUI_URL="${OWUI_URL:-http://localhost:8080}"

SNAP_FILE="${OWUI_SNAP:-}"
SNAP_CHANNEL="${OWUI_CHANNEL:-}"
GEMMA4_CHANNEL="${GEMMA4_CHANNEL:-stable}"

# ---------------------------------------------------------------------------
# Validate/normalise the environment-driven configuration (shared by both
# runners). Aborts if the install source is missing or ambiguous.
# ---------------------------------------------------------------------------
resolve_config() {
  if [[ -z "$SNAP_FILE" && -z "$SNAP_CHANNEL" ]]; then
    echo "Error: set either OWUI_SNAP (local file) or OWUI_CHANNEL (store channel)." >&2
    exit 1
  fi
  if [[ -n "$SNAP_FILE" && -n "$SNAP_CHANNEL" ]]; then
    echo "Error: OWUI_SNAP and OWUI_CHANNEL are mutually exclusive." >&2
    exit 1
  fi

  if [[ -n "$SNAP_FILE" ]]; then
    SNAP_FILE="$(realpath "$SNAP_FILE")"
  fi
}

# Return success when OWUI_CLEANUP is set to a truthy value.
cleanup_enabled() {
  case "${OWUI_CLEANUP:-}" in
    1|true|TRUE|True|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Wait for the open-webui server to stabilise after connecting the interface.
#
# Connecting open-webui:config triggers the import-shared-configs hook, which
# may rewrite the DB and restart snap.open-webui.server. If tests start during
# that window, an in-flight request dies with "Connection reset by peer".
#
# We consider the server stable once its systemd MainPID has stayed unchanged
# while /health returns 200 for STABLE_WINDOW consecutive seconds.
# ---------------------------------------------------------------------------
wait_for_server_stable() {
  local stable_window=15
  local timeout=180
  local url="${OWUI_URL}/health"
  echo "=== Waiting for open-webui server to stabilise ==="

  local deadline=$(( SECONDS + timeout ))
  local last_pid="" stable_since=0
  while (( SECONDS < deadline )); do
    local pid
    pid="$(systemctl show -p MainPID --value snap.open-webui.server.service 2>/dev/null || echo 0)"
    local healthy=false
    if [[ "$pid" != "0" && -n "$pid" ]] \
       && curl -fsS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
      healthy=true
    fi

    if [[ "$healthy" == true && "$pid" == "$last_pid" ]]; then
      if (( SECONDS - stable_since >= stable_window )); then
        echo "=== Server stable (pid $pid) ==="
        return 0
      fi
    else
      last_pid="$pid"
      stable_since=$SECONDS
    fi
    sleep 1
  done

  echo "=== Warning: server did not stabilise within ${timeout}s; continuing anyway ===" >&2
  return 0
}

# ---------------------------------------------------------------------------
# Resolve Python / pytest (prefer repo venv, fall back to system)
# ---------------------------------------------------------------------------
activate_venv() {
  if [[ ! -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
    python3 -m venv "${REPO_ROOT}/.venv"
  fi
  source "${REPO_ROOT}/.venv/bin/activate"
}

# ---------------------------------------------------------------------------
# Cleanup: remove snaps we installed (restores host to prior state)
# ---------------------------------------------------------------------------
cleanup() {
  echo ""
  echo "=== Removing open-webui ==="
  sudo snap remove --purge open-webui 2>/dev/null || true
  echo "=== Removing gemma4 ==="
  sudo snap remove --purge gemma4 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Install helpers
# ---------------------------------------------------------------------------
install_owui_target() {
  if [[ -n "$SNAP_FILE" ]]; then
    echo "=== Installing open-webui from $(basename "$SNAP_FILE") ==="
    sudo snap install --dangerous "$SNAP_FILE"
  else
    echo "=== Installing open-webui from channel $SNAP_CHANNEL ==="
    sudo snap install open-webui --channel="$SNAP_CHANNEL"
  fi
}

install_gemma4() {
  echo "=== Installing gemma4 from channel $GEMMA4_CHANNEL ==="
  sudo snap install gemma4 --channel="$GEMMA4_CHANNEL"
}

connect_interface() {
  echo "=== Connecting snap interface ==="
  sudo snap connect open-webui:config gemma4:open-webui
}

install_test_deps() {
  echo "=== Installing test dependencies ==="
  python3 -m pip install --quiet -r "$REPO_ROOT/tests/smoke/requirements.txt"
}

# ---------------------------------------------------------------------------
# Dump logs (call on failure)
# ---------------------------------------------------------------------------
dump_logs() {
  echo ""
  echo "=== journalctl snap.open-webui.server ==="
  journalctl -u snap.open-webui.server --no-pager -n 500 || true
  echo ""
  echo "=== snap logs open-webui ==="
  sudo snap logs open-webui -n 100 || true
}
