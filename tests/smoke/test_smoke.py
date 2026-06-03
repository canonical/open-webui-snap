import pytest


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


@pytest.mark.skip(reason="step 4")
def test_version_matches_requirements(client):
    """GET /api/config reports the version pinned in dependencies/requirements.txt."""
    pass


@pytest.mark.skip(reason="step 4")
def test_gemma4_model_registered(client):
    """GET /api/models eventually lists a gemma model (OpenAI-compatible endpoint)."""
    pass


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
