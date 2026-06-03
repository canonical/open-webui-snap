import time

import pytest

from conftest import MODELS_TIMEOUT, POLL_INTERVAL


def test_server_not_crashed(server_ready):
    """Server logs contain no traceback while port 8080 comes up."""
    journal = server_ready["journal"]
    assert "Traceback" not in journal, (
        f"Python traceback found in snap.open-webui.server journal:\n{journal}"
    )


def test_ui_reachable(server_ready, client):
    """GET /health returns 200."""
    r = client.get(f"{client.base_url}/health")
    assert r.status_code == 200, (
        f"GET /health returned {r.status_code}: {r.text}"
    )


def test_admin_signup(admin_token):
    """POST /api/v1/auths/signup succeeds and returns a token."""
    assert admin_token, "Admin signup did not return a usable JWT token"


def test_version_matches_requirements(auth_client, pinned_version):
    """GET /api/config reports the version pinned in dependencies/requirements.txt."""
    r = auth_client.get(f"{auth_client.base_url}/api/config")
    assert r.status_code == 200, f"GET /api/config returned {r.status_code}: {r.text}"
    data = r.json()
    reported = data.get("version", "")
    assert reported == pinned_version, (
        f"Version mismatch: snap reports '{reported}', "
        f"dependencies/requirements.txt pins '{pinned_version}'"
    )


def test_gemma4_model_registered(auth_client):
    """GET /api/models eventually lists a gemma model (OpenAI-compatible endpoint)."""
    deadline = time.monotonic() + MODELS_TIMEOUT
    while time.monotonic() < deadline:
        r = auth_client.get(f"{auth_client.base_url}/api/models")
        if r.status_code == 200:
            models = r.json().get("data", [])
            gemma_models = [m for m in models if "gemma" in m.get("id", "").lower()]
            if gemma_models:
                return  # at least one gemma model registered — pass
        time.sleep(POLL_INTERVAL)

    # Timed out — surface what was visible
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


@pytest.mark.skip(reason="step 5")
def test_text_prompt(client):
    """POST /api/chat/completions with a text message returns a non-empty reply."""
    pass


@pytest.mark.skip(reason="step 5")
def test_image_prompt(client):
    """POST /api/chat/completions with an image_url content part returns a non-empty reply."""
    pass


@pytest.mark.skip(reason="step 5")
def test_audio_transcription(client):
    """POST /api/v1/audio/transcriptions with fixture WAV returns a transcript containing 'fox'."""
    pass


@pytest.mark.skip(reason="step 6")
def test_pdf_rag(client):
    """Upload fixture PDF, wait for indexing, prompt about it, assert RAG answer is correct."""
    pass
