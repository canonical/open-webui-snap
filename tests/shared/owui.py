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
# How long to wait for the gemma4 model to *disappear* after stopping the gemma4
# snap.  The plugin re-scans ports on each /api/models call and the model cache
# TTL is ~1s, so removal is quick once the port stops listening; allow a modest
# margin for the snap to fully stop.
MODEL_REMOVAL_TIMEOUT = 120  # 2 min
POLL_INTERVAL = 5

# Per-request (connect, read) timeouts in seconds, shared by the smoke and
# upgrade suites.  Without these a stalled server (e.g. a hung model inference)
# would block a ``requests`` call forever.
QUICK_TIMEOUT = (10, 30)       # lightweight JSON endpoints
INFERENCE_TIMEOUT = (10, 600)  # model generation / file upload & processing

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
        timeout=30,
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
        timeout=30,
    )
    token = resp.json().get("token") if resp.status_code == 200 else None
    return resp, token


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def _list_model_ids(client, base_url: str, refresh: bool = False):
    """Return the list of model ids from /api/models, or raise RequestException.

    Open WebUI 0.11.0 caches the base model list (``models.base_models_cache``,
    enabled by default) in ``app.state.BASE_MODELS``.  A plain ``GET /api/models``
    returns that cache and only re-invokes the inference-snaps plugin's port scan
    when called with ``?refresh=true``.  Callers that depend on *live* discovery
    (a snap appearing or disappearing) must pass ``refresh=True`` to bypass the
    cache, otherwise a stopped snap's model lingers until the cache is refreshed.
    """
    params = {"refresh": "true"} if refresh else None
    r = client.get(f"{base_url}/api/models", params=params, timeout=10)
    if r.status_code == 200:
        return [m.get("id", "") for m in r.json().get("data", [])]
    return None


def wait_for_gemma_model(client, base_url: str, timeout: int = MODELS_TIMEOUT):
    """Poll ``GET /api/models`` until a gemma model appears; return its id or None.

    The inference-snaps plugin discovers gemma4 by scanning local ports, so the
    model appears once gemma4's server is up and serving. RequestException is
    swallowed and retried.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ids = _list_model_ids(client, base_url, refresh=True)
            if ids is not None:
                for mid in ids:
                    if "gemma" in mid.lower():
                        return mid
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return None


def _model_unusable(client, base_url: str, model_id: str) -> bool:
    """Return True only if a chat completion to *model_id* fails to serve a reply.

    Used to detect that a stopped snap's model can no longer be served even when
    it still lingers in ``/api/models``: the plugin's ``pipe()`` hits a closed
    port and Open WebUI returns HTTP 200 with an ``{"error": ...}`` body (no
    ``choices``) instead of a completion.

    A read timeout is deliberately NOT treated as unusable: a slow-but-alive
    model on a loaded CI runner can legitimately take a while to respond. The
    ``(connect, read)`` timeout keeps the connect phase short (a closed port
    fails fast) while allowing a slow reply.
    """
    try:
        r = client.post(
            f"{base_url}/api/chat/completions",
            json={
                "model": model_id,
                "chat_id": "local:model-removal-probe",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=(5, 60),
        )
    except requests.exceptions.Timeout:
        # Alive but slow to answer — inconclusive, keep polling.
        return False
    except requests.exceptions.ConnectionError:
        # Port closed / server gone.
        return True
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return True
    try:
        data = r.json()
    except ValueError:
        return True
    # A served completion has a non-empty "choices" list; an error reply does not.
    return "error" in data or not data.get("choices")


def wait_for_model_absent(client, base_url: str, substr: str = "gemma",
                          model_id: str | None = None,
                          timeout: int = MODEL_REMOVAL_TIMEOUT):
    """Poll until the stopped snap's model is gone from ``/api/models`` *or* unusable.

    Returns True once the model has been removed, or the last-seen list of ids if
    the deadline was reached (so callers can produce a useful diagnostic).

    Two things can happen after a snap stops, depending on what else is running:

    * If other inference snaps remain, the plugin still returns a non-empty model
      list and the stopped model disappears from ``/api/models``.
    * If the stopped snap was the *only* backend (as in CI), the plugin returns
      an empty list and Open WebUI's ``get_all_models`` falls back to its cached
      ``BASE_MODELS``, so the model lingers indefinitely.  In that case we detect
      removal by confirming the model can no longer serve a request.

    RequestException while the model server is stopping is swallowed.
    """
    deadline = time.monotonic() + timeout
    last_ids = None
    while time.monotonic() < deadline:
        try:
            ids = _list_model_ids(client, base_url, refresh=True)
            if ids is not None:
                last_ids = ids
                if not any(substr in mid.lower() for mid in ids):
                    return True
                # Model still listed (stale cache): treat as removed once it can
                # no longer serve a completion.
                stale = model_id or next(
                    (mid for mid in ids if substr in mid.lower()), None
                )
                if stale is not None and _model_unusable(client, base_url, stale):
                    return True
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return last_ids if last_ids is not None else []


# ---------------------------------------------------------------------------
# Bundled plugins (seeded functions)
# ---------------------------------------------------------------------------
# The snap seeds bundled Open WebUI plugins (functions) via the seed-plugins
# oneshot daemon.  This id is derived from plugins/inference-snaps-plugin.py by
# scripts/seed-plugins.py (non-identifier chars -> '_', lowercased).
SNAP_PLUGIN_ID = "inference_snaps_plugin"


def wait_for_seeded_function(client, base_url: str, function_id: str = SNAP_PLUGIN_ID,
                             timeout: int = MODELS_TIMEOUT):
    """Poll ``GET /api/v1/functions/`` until *function_id* appears.

    The seed-plugins daemon runs after the server and polls for the database, so
    the function may take a short while to show up.  Returns the matching
    function dict, or None if the deadline was reached.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = client.get(f"{base_url}/api/v1/functions/", timeout=10)
            if r.status_code == 200:
                for func in r.json():
                    if func.get("id") == function_id:
                        return func
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return None


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
