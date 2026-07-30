import base64
import os
import pathlib
import sys

import pytest
import requests

# Make the shared helper module (tests/shared/owui.py) importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import owui

# Directory that holds fixture files committed to the repo
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


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
    result = owui.wait_for_health(base_url)
    if result is None:
        pytest.fail(
            f"Server did not become healthy within {owui.HEALTH_TIMEOUT}s.\n\n"
            f"Journal tail:\n{owui.get_journal()}"
        )
    return result


@pytest.fixture(scope="session")
def admin_token(client, server_ready):
    """Sign up the first admin account and return its JWT.

    Open WebUI lets the very first signup become admin with no prior auth.
    """
    resp, token = owui.signup_admin(client, client.base_url)
    assert resp.status_code == 200, (
        f"Admin signup returned {resp.status_code}: {resp.text}"
    )
    assert token, f"No 'token' key in signup response: {resp.text}"
    return token


@pytest.fixture(scope="session")
def auth_client(client, admin_token):
    """requests.Session with the admin Bearer token pre-set."""
    client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return client


@pytest.fixture(scope="session")
def pinned_version():
    """Read the open-webui version pinned in dependencies/requirements.txt.

    Returns a plain version string, e.g. '0.9.2'.
    """
    version = owui.read_pinned_version()
    if version is None:
        pytest.fail(
            "Could not find open-webui version in dependencies/requirements.txt"
        )
    return version


@pytest.fixture(scope="session")
def gemma_model_id(auth_client):
    """Poll GET /api/models until a gemma model appears; return its id.

    Used by both test_gemma4_model_registered and the step-5 prompt tests.
    Hard cap: MODELS_TIMEOUT (15 min) per the smoke-test plan.
    """
    model_id = owui.wait_for_gemma_model(auth_client, auth_client.base_url)
    if model_id is None:
        pytest.fail(
            f"No gemma model appeared in /api/models within {owui.MODELS_TIMEOUT}s."
        )
    return model_id


@pytest.fixture(scope="session")
def image_b64() -> str:
    """Base64-encoded content of the fixture JPEG (no data-URI prefix)."""
    return base64.b64encode((FIXTURES_DIR / "circle.jpg").read_bytes()).decode()


@pytest.fixture(scope="session")
def audio_path() -> pathlib.Path:
    """Absolute path to the fixture MP3 file."""
    return FIXTURES_DIR / "audio.mp3"


@pytest.fixture(scope="session")
def rag_pdf_path() -> pathlib.Path:
    """Absolute path to the fixture PDF used for the RAG test."""
    return FIXTURES_DIR / "CC-BY-SA-4.0.pdf"
