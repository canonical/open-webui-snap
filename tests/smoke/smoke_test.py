import subprocess
import time

import pytest
import requests

import owui
from owui import INFERENCE_TIMEOUT, QUICK_TIMEOUT

RAG_PROCESS_TIMEOUT = 120  # 2 min hard cap for PDF indexing


def test_server_not_crashed(server_ready):
    """Server logs contain no traceback while port 8080 comes up."""
    journal = server_ready["journal"]
    assert "Traceback" not in journal, (
        f"Python traceback found in snap.open-webui.server journal:\n{journal}"
    )


def test_ui_reachable(server_ready, client):
    """GET /health returns 200."""
    r = client.get(f"{client.base_url}/health", timeout=QUICK_TIMEOUT)
    assert r.status_code == 200, (
        f"GET /health returned {r.status_code}: {r.text}"
    )


def test_admin_signup(admin_token):
    """POST /api/v1/auths/signup succeeds and returns a token."""
    assert admin_token, "Admin signup did not return a usable JWT token"


def test_snap_plugin_seeded(auth_client):
    """The bundled inference-snaps plugin is seeded and active by default."""
    func = owui.wait_for_seeded_function(auth_client, auth_client.base_url)
    assert func is not None, (
        f"Bundled plugin '{owui.SNAP_PLUGIN_ID}' did not appear in "
        f"/api/v1/functions/ within {owui.MODELS_TIMEOUT}s"
    )
    assert func.get("type") == "pipe", (
        f"Expected seeded plugin to be a 'pipe', got: {func.get('type')!r}"
    )
    assert func.get("is_active") is True, (
        f"Seeded plugin '{owui.SNAP_PLUGIN_ID}' should be active by default: {func}"
    )


def test_version_matches_requirements(auth_client, pinned_version):
    """GET /api/config reports the version pinned in dependencies/requirements.txt."""
    r = auth_client.get(f"{auth_client.base_url}/api/config", timeout=QUICK_TIMEOUT)
    assert r.status_code == 200, f"GET /api/config returned {r.status_code}: {r.text}"
    data = r.json()
    reported = data.get("version", "")
    assert reported == pinned_version, (
        f"Version mismatch: snap reports '{reported}', "
        f"dependencies/requirements.txt pins '{pinned_version}'"
    )


def test_gemma4_model_registered(gemma_model_id):
    """GET /api/models eventually lists a gemma model (OpenAI-compatible endpoint)."""
    assert gemma_model_id, "No gemma model ID returned"


def test_text_prompt(auth_client, gemma_model_id):
    """POST /api/chat/completions with a text message returns a non-empty reply."""
    r = auth_client.post(
        f"{auth_client.base_url}/api/chat/completions",
        json={
            "model": gemma_model_id,
            # Use a "local:" chat_id so Open WebUI treats this as a temporary
            # (non-persisted) chat.  Without any chat_id the metadata dict
            # contains {"chat_id": None}, and OW 0.9.5's get_event_emitter()
            # crashes because dict.get("chat_id", "") still returns None when
            # the key exists — None.startswith() raises AttributeError → 400.
            "chat_id": "local:smoke-text",
            "messages": [{"role": "user", "content": "Reply with the single word PONG."}],
        },
        timeout=INFERENCE_TIMEOUT,
    )
    assert r.status_code == 200, f"chat/completions returned {r.status_code}: {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    assert content.strip(), "Text prompt returned an empty response"


def test_image_prompt(auth_client, gemma_model_id, image_b64):
    """POST /api/chat/completions with an image_url content part returns a non-empty reply."""
    r = auth_client.post(
        f"{auth_client.base_url}/api/chat/completions",
        json={
            "model": gemma_model_id,
            "chat_id": "local:smoke-image",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image in one sentence."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        },
        timeout=INFERENCE_TIMEOUT,
    )
    assert r.status_code == 200, f"chat/completions (image) returned {r.status_code}: {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    assert content.strip(), "Image prompt returned an empty response"
    assert "circle" in content.lower() or "circular" in content.lower(), (
        f"Expected 'circle' or 'circular' in image response, got: {content!r}"
    )


def test_audio_transcription(auth_client, audio_path):
    """POST /api/v1/audio/transcriptions with fixture WAV returns a transcript containing 'fox'."""
    with open(audio_path, "rb") as fh:
        r = auth_client.post(
            f"{auth_client.base_url}/api/v1/audio/transcriptions",
            files={"file": ("audio.mp3", fh, "audio/mpeg")},
            timeout=INFERENCE_TIMEOUT,
        )
    assert r.status_code == 200, f"audio/transcriptions returned {r.status_code}: {r.text}"
    transcript = r.json().get("text", "")
    assert "fox" in transcript.lower(), (
        f"Expected 'fox' in transcript, got: {transcript!r}"
    )


def test_pdf_rag(auth_client, gemma_model_id, rag_pdf_path):
    """Upload CC-BY-SA-4.0.pdf, wait for indexing, ask for a summary, assert 'license' in reply."""
    # 1. Upload the PDF
    with open(rag_pdf_path, "rb") as fh:
        r = auth_client.post(
            f"{auth_client.base_url}/api/v1/files/",
            files={"file": ("CC-BY-SA-4.0.pdf", fh, "application/pdf")},
            timeout=INFERENCE_TIMEOUT,
        )
    assert r.status_code == 200, f"File upload returned {r.status_code}: {r.text}"
    file_id = r.json().get("id")
    assert file_id, f"No 'id' in upload response: {r.json()}"

    # 2. Poll until indexing is complete.
    # The server is a single worker and can briefly drop connections while it is
    # busy generating embeddings / writing to the vector DB, so tolerate
    # transient connection errors and keep polling until the deadline.
    deadline = time.monotonic() + RAG_PROCESS_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        try:
            s = auth_client.get(
                f"{auth_client.base_url}/api/v1/files/{file_id}/process/status",
                timeout=QUICK_TIMEOUT,
            )
        except requests.exceptions.ConnectionError as exc:
            last = f"connection error: {exc}"
            time.sleep(owui.POLL_INTERVAL)
            continue
        last = s.json() if s.status_code == 200 else s.text
        if s.status_code == 200 and s.json().get("status") == "completed":
            break
        time.sleep(owui.POLL_INTERVAL)
    else:
        pytest.fail(
            f"PDF indexing did not complete within {RAG_PROCESS_TIMEOUT}s. "
            f"Last status response: {last}"
        )

    # 3. Chat completion referencing the uploaded file
    r = auth_client.post(
        f"{auth_client.base_url}/api/chat/completions",
        json={
            "model": gemma_model_id,
            "chat_id": "local:smoke-rag",
            "messages": [
                {"role": "user", "content": "Please summarise the document."}
            ],
            "files": [{"type": "file", "id": file_id}],
        },
        timeout=INFERENCE_TIMEOUT,
    )
    assert r.status_code == 200, f"chat/completions (RAG) returned {r.status_code}: {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    assert "license" in content.lower(), (
        f"Expected 'license' in RAG response, got: {content!r}"
    )


# ---------------------------------------------------------------------------
# Model removal — must run LAST, after every model-dependent test.
# ---------------------------------------------------------------------------
def test_model_disappears_after_stopping_snap(auth_client, gemma_model_id):
    """Stopping the gemma4 snap removes its auto-discovered model from /api/models.

    With the inference-snaps plugin, models are discovered by scanning local
    ports on each /api/models call.  Once gemma4's server stops listening, the
    plugin no longer finds it, so the model disappears without any interface
    disconnect.  Connection errors during the brief stop window are tolerated.
    """
    subprocess.run(["sudo", "snap", "stop", "gemma4"], check=True)
    try:
        result = owui.wait_for_model_absent(auth_client, auth_client.base_url)
        assert result is True, (
            f"gemma model did not disappear from /api/models within "
            f"{owui.MODEL_REMOVAL_TIMEOUT}s after stopping the gemma4 snap. "
            f"Models still visible: {result}"
        )
    finally:
        # Restore gemma4 so local re-runs (without OWUI_CLEANUP) start clean.
        subprocess.run(["sudo", "snap", "start", "gemma4"], check=False)
