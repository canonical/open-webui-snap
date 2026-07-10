import subprocess
import time

import pytest

import owui

RAG_PROCESS_TIMEOUT = 120  # 2 min hard cap for PDF indexing


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
        )
    assert r.status_code == 200, f"File upload returned {r.status_code}: {r.text}"
    file_id = r.json().get("id")
    assert file_id, f"No 'id' in upload response: {r.json()}"

    # 2. Poll until indexing is complete
    deadline = time.monotonic() + RAG_PROCESS_TIMEOUT
    while time.monotonic() < deadline:
        s = auth_client.get(f"{auth_client.base_url}/api/v1/files/{file_id}/process/status")
        if s.status_code == 200 and s.json().get("status") == "completed":
            break
        time.sleep(owui.POLL_INTERVAL)
    else:
        last = s.json() if s.status_code == 200 else s.text
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
    )
    assert r.status_code == 200, f"chat/completions (RAG) returned {r.status_code}: {r.text}"
    content = r.json()["choices"][0]["message"]["content"]
    assert "license" in content.lower(), (
        f"Expected 'license' in RAG response, got: {content!r}"
    )


# ---------------------------------------------------------------------------
# Interface disconnect — must run LAST, after every model-dependent test.
# ---------------------------------------------------------------------------
def test_model_disappears_after_disconnect(auth_client, gemma_model_id):
    """Disconnecting gemma4 removes the model from /api/models.

    The gemma service can take >1 min to notice the disconnect, after which it
    triggers an Open WebUI restart, so we poll with a generous cap and tolerate
    connection errors during the restart window.
    """
    subprocess.run(
        ["sudo", "snap", "disconnect", "open-webui:config", "gemma4:open-webui"],
        check=True,
    )
    result = owui.wait_for_model_absent(auth_client, auth_client.base_url)
    assert result is True, (
        f"gemma model did not disappear from /api/models within "
        f"{owui.DISCONNECT_TIMEOUT}s after disconnecting the interface. "
        f"Models still visible: {result}"
    )
