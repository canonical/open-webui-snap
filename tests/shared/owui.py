"""Shared helpers for the Open WebUI snap test suites.

Both the smoke suite (``tests/smoke``) and the upgrade suite
(``tests/upgrade``) import from this module so the polling, auth and version
logic lives in exactly one place.

The functions here are plain helpers (no pytest fixtures) so they can be reused
from fixtures *and* from tests that drive snap operations directly.
"""

import datetime
import pathlib
import subprocess
import time

import requests

# ---------------------------------------------------------------------------
# Timeouts / constants
# ---------------------------------------------------------------------------
# First-time installs download embedding models from HuggingFace, which can be
# slow — allow up to 20 min so CI and local first-runs don't time out.
HEALTH_TIMEOUT = 1200  # 20 min
# How long to wait for the gemma4 model to appear in /api/models (seconds).
MODELS_TIMEOUT = 900   # 15 min hard cap per smoke-test plan
# How long to wait for the gemma4 model to *disappear* after disconnecting the
# interface.  The gemma service can take >1 min to notice the disconnect, after
# which it triggers an Open WebUI restart, so allow a generous window.
DISCONNECT_TIMEOUT = 300  # 5 min
POLL_INTERVAL = 5

ADMIN_NAME = "Smoke Admin"
ADMIN_EMAIL = "admin@smoke.test"
ADMIN_PASSWORD = "SmokeTest1234!"

# Record when the importing process started so journalctl can be scoped to it,
# avoiding false "Traceback" hits from previous runs on the same host.
_SESSION_START = datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
def get_journal(lines: int = 500) -> str:
    """Return recent journalctl output for snap.open-webui.server.

    Scoped to logs produced since this process started so that tracebacks from
    previous runs on the same host don't cause false failures.
    """
    since = _SESSION_START.strftime("%Y-%m-%d %H:%M:%S")
    result = subprocess.run(
        [
            "journalctl",
            "-u", "snap.open-webui.server",
            "--no-pager",
            "--since", since,
            "-n", str(lines),
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def wait_for_health(base_url: str, timeout: int = HEALTH_TIMEOUT) -> dict:
    """Poll ``GET /health`` until 200 or ``timeout``.

    Returns ``{"healthy": True, "journal": <str>}`` on success, or ``None`` if
    the deadline was reached (caller decides how to fail).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=5)
            if r.status_code == 200:
                return {"healthy": True, "journal": get_journal()}
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def signup_admin(client, base_url: str):
    """Sign up the first admin account and return (response, token).

    Open WebUI lets the very first signup become admin with no prior auth.
    Only works on a fresh database.
    """
    resp = client.post(
        f"{base_url}/api/v1/auths/signup",
        json={
            "name": ADMIN_NAME,
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
    )
    token = resp.json().get("token") if resp.status_code == 200 else None
    return resp, token


def login_admin(client, base_url: str):
    """Log in with the known admin credentials and return (response, token).

    Used after an upgrade, where the account already exists (so signup would
    fail) and reusing a pre-upgrade JWT may be invalid if token signing
    changed across the upgrade.
    """
    resp = client.post(
        f"{base_url}/api/v1/auths/signin",
        json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
    )
    token = resp.json().get("token") if resp.status_code == 200 else None
    return resp, token


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def _list_model_ids(client, base_url: str):
    """Return the list of model ids from /api/models, or raise RequestException."""
    r = client.get(f"{base_url}/api/models", timeout=10)
    if r.status_code == 200:
        return [m.get("id", "") for m in r.json().get("data", [])]
    return None


def wait_for_gemma_model(client, base_url: str, timeout: int = MODELS_TIMEOUT):
    """Poll ``GET /api/models`` until a gemma model appears; return its id or None.

    The background service that registers the gemma4 endpoint triggers a server
    restart, so RequestException (connection refused during the restart window)
    is swallowed and retried.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ids = _list_model_ids(client, base_url)
            if ids is not None:
                for mid in ids:
                    if "gemma" in mid.lower():
                        return mid
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return None


def wait_for_model_absent(client, base_url: str, substr: str = "gemma",
                          timeout: int = DISCONNECT_TIMEOUT):
    """Poll ``GET /api/models`` until no model id contains ``substr``.

    Returns True once the model is gone, or the last-seen list of ids if the
    deadline was reached (so callers can produce a useful diagnostic).
    RequestException during the disconnect-triggered restart is swallowed.
    """
    deadline = time.monotonic() + timeout
    last_ids = None
    while time.monotonic() < deadline:
        try:
            ids = _list_model_ids(client, base_url)
            if ids is not None:
                last_ids = ids
                if not any(substr in mid.lower() for mid in ids):
                    return True
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return last_ids if last_ids is not None else []


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
def read_pinned_version() -> str:
    """Read the open-webui version pinned in dependencies/requirements.txt.

    Returns a plain version string (e.g. '0.9.2') or None if not found.
    """
    req_file = (
        pathlib.Path(__file__).resolve().parent  # tests/shared/
        .parent                                   # tests/
        .parent                                   # repo root
        / "dependencies"
        / "requirements.txt"
    )
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if line.lower().startswith("open-webui=="):
            return line.split("==", 1)[1].strip()
    return None
