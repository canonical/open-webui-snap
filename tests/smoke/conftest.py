import base64
import os
import pathlib
import subprocess
import time

import pytest
import requests

# Directory that holds fixture files committed to the repo
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# How long to wait for the server to become healthy (seconds)
HEALTH_TIMEOUT = 600   # 10 min — model cold-start can be slow
# How long to wait for gemma4 model to appear in /api/models (seconds)
MODELS_TIMEOUT = 900   # 15 min hard cap per smoke-test plan
POLL_INTERVAL = 5

ADMIN_NAME = "Smoke Admin"
ADMIN_EMAIL = "admin@smoke.test"
ADMIN_PASSWORD = "SmokeTest1234!"


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("OWUI_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def client(base_url):
    s = requests.Session()
    s.base_url = base_url
    return s


@pytest.fixture(scope="session")
def server_ready(base_url):
    """Poll GET /health until 200 or HEALTH_TIMEOUT.

    Returns a dict with:
      - healthy (bool): True when /health returned 200
      - journal (str): recent journalctl output for snap.open-webui.server
    """
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_exc = None

    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/health", timeout=5)
            if r.status_code == 200:
                journal = _get_journal()
                return {"healthy": True, "journal": journal}
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(POLL_INTERVAL)

    journal = _get_journal()
    pytest.fail(
        f"Server did not become healthy within {HEALTH_TIMEOUT}s. "
        f"Last error: {last_exc}\n\nJournal tail:\n{journal}"
    )


def _get_journal(lines: int = 500) -> str:
    result = subprocess.run(
        [
            "journalctl",
            "-u", "snap.open-webui.server",
            "--no-pager",
            "-n", str(lines),
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout


@pytest.fixture(scope="session")
def admin_token(client, server_ready):
    """Sign up the first admin account and return its JWT.

    Open WebUI lets the very first signup become admin with no prior auth.
    """
    resp = client.post(
        f"{client.base_url}/api/v1/auths/signup",
        json={
            "name": ADMIN_NAME,
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, (
        f"Admin signup returned {resp.status_code}: {resp.text}"
    )
    data = resp.json()
    token = data.get("token")
    assert token, f"No 'token' key in signup response: {data}"
    return token


@pytest.fixture(scope="session")
def auth_client(client, admin_token):
    """requests.Session with the admin Bearer token pre-set."""
    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return client


@pytest.fixture(scope="session")
def pinned_version():
    """Read the open-webui version pinned in dependencies/requirements.txt.

    Navigates from this file's location (tests/smoke/) up to the repo root.
    Returns a plain version string, e.g. '0.9.2'.
    """
    import pathlib
    req_file = (
        pathlib.Path(__file__).parent  # tests/smoke/
        .parent                        # tests/
        .parent                        # repo root
        / "dependencies"
        / "requirements.txt"
    )
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if line.lower().startswith("open-webui=="):
            return line.split("==", 1)[1].strip()
    pytest.fail(f"Could not find open-webui version in {req_file}")


@pytest.fixture(scope="session")
def gemma_model_id(auth_client):
    """Poll GET /api/models until a gemma model appears; return its id.

    Used by both test_gemma4_model_registered and the step-5 prompt tests.
    Hard cap: MODELS_TIMEOUT (15 min) per the smoke-test plan.
    """
    deadline = time.monotonic() + MODELS_TIMEOUT
    while time.monotonic() < deadline:
        r = auth_client.get(f"{auth_client.base_url}/api/models")
        if r.status_code == 200:
            models = r.json().get("data", [])
            for m in models:
                if "gemma" in m.get("id", "").lower():
                    return m["id"]
        time.sleep(POLL_INTERVAL)

    r = auth_client.get(f"{auth_client.base_url}/api/models")
    available = (
        [m.get("id") for m in r.json().get("data", [])]
        if r.status_code == 200
        else [f"(HTTP {r.status_code})"]
    )
    pytest.fail(
        f"No gemma model appeared in /api/models within {MODELS_TIMEOUT}s. "
        f"Models visible at timeout: {available}"
    )


@pytest.fixture(scope="session")
def image_b64() -> str:
    """Base64-encoded content of the fixture JPEG (no data-URI prefix)."""
    return base64.b64encode((FIXTURES_DIR / "circle.jpg").read_bytes()).decode()


@pytest.fixture(scope="session")
def audio_path() -> pathlib.Path:
    """Absolute path to the fixture MP3 file."""
    return FIXTURES_DIR / "audio.mp3"

