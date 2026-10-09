"""Upgrade scenario for the Open WebUI snap.

Driven by tests/upgrade/run.sh.  By the time
this module runs, the harness has already:

  * removed and purged open-webui,
  * installed open-webui from the baseline channel (OWUI_FROM_CHANNEL,
    default stable).

gemma4 itself stays installed throughout.  These tests run **in file order**
inside a single pytest session:

  1. baseline server healthy
  2. first-signup admin (fresh, purged DB)
  3. configure Gemma4 through the API (also works on legacy baselines)
  4. gemma model present on the baseline build
  5. text prompt works on the baseline build
  6. upgrade (refresh) to the provided snap file / channel
  7. fresh login on the upgraded build (old JWT is intentionally not reused)
  8. reported version matches dependencies/requirements.txt
  9. gemma model still present after the upgrade
  10. verify the baseline config survived, then hand over to
     open-webui:provider <-> gemma4:provider
  11. text prompt still works after the upgrade
"""

import os
import subprocess

import pytest

import owui
from owui import INFERENCE_TIMEOUT, QUICK_TIMEOUT


def _set_bearer(client, token):
    client.headers.update({"Authorization": f"Bearer {token}"})


def _text_prompt(client, model_id, chat_id):
    """POST a simple text prompt; return the reply content (asserts 200)."""
    r = client.post(
        f"{client.base_url}/api/chat/completions",
        json={
            "model": model_id,
            "chat_id": chat_id,
            "messages": [
                {"role": "user", "content": "Reply with the single word PONG."}
            ],
        },
        timeout=INFERENCE_TIMEOUT,
    )
    assert r.status_code == 200, (
        f"chat/completions returned {r.status_code}: {r.text}"
    )
    return r.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Stable install (pre-upgrade)
# ---------------------------------------------------------------------------
def test_stable_server_ready(client):
    """The freshly-installed baseline build becomes healthy."""
    result = owui.wait_for_health(client.base_url)
    assert result is not None, (
        f"Stable server did not become healthy within {owui.HEALTH_TIMEOUT}s.\n\n"
        f"Journal tail:\n{owui.get_journal()}"
    )


def test_stable_admin_signup(client):
    """First-signup succeeds on the purged/fresh DB and returns a token."""
    resp, token = owui.signup_admin(client, client.base_url)
    assert resp.status_code == 200, (
        f"Admin signup returned {resp.status_code}: {resp.text}"
    )
    assert token, f"No token in signup response: {resp.text}"
    _set_bearer(client, token)


def test_configure_baseline_provider(client, state):
    """Seed an untagged endpoint so legacy importers do not remove it."""
    state["original_openai_config"] = owui.get_openai_config(client)
    state["provider_url"] = owui.wait_for_provider_url()
    owui.update_openai_config(client, {
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": [state["provider_url"]],
        "OPENAI_API_KEYS": [""],
        "OPENAI_API_CONFIGS": {
            "0": {"enable": True, "auth_type": "none"},
        },
    })


def test_stable_gemma_model_present(client, state):
    """A gemma model is registered in /api/models on the baseline build."""
    model_id = owui.wait_for_gemma_model(client, client.base_url)
    assert model_id, (
        f"No gemma model appeared in /api/models within {owui.MODELS_TIMEOUT}s "
        f"on the baseline build."
    )
    state["model_id"] = model_id


def test_stable_text_prompt(client, state):
    """A text prompt returns a non-empty reply on the baseline build."""
    content = _text_prompt(client, state["model_id"], "local:upgrade-stable-text")
    assert content.strip(), "Text prompt returned an empty response on stable"


# ---------------------------------------------------------------------------
# Perform the upgrade
# ---------------------------------------------------------------------------
def test_perform_upgrade(client):
    """Refresh open-webui to the provided snap file / channel, then wait healthy.

    The upgrade target is passed by the harness via env:
      * OWUI_UPGRADE_SNAP=<file>     -> sudo snap install --dangerous <file>
      * OWUI_UPGRADE_CHANNEL=<chan>  -> sudo snap refresh open-webui --channel <chan>

    The baseline endpoint is configured through the API because older builds
    cannot connect to Gemma4's inference-provider slot.
    """
    snap_file = os.environ.get("OWUI_UPGRADE_SNAP")
    channel = os.environ.get("OWUI_UPGRADE_CHANNEL")

    if snap_file:
        cmd = ["sudo", "snap", "install", "--dangerous", snap_file]
    elif channel:
        cmd = ["sudo", "snap", "refresh", "open-webui", "--channel", channel]
    else:
        pytest.fail(
            "Neither OWUI_UPGRADE_SNAP nor OWUI_UPGRADE_CHANNEL is set; "
            "cannot determine the upgrade target."
        )

    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"Upgrade command {cmd} failed ({proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    result = owui.wait_for_health(client.base_url)
    assert result is not None, (
        f"Upgraded server did not become healthy within {owui.HEALTH_TIMEOUT}s.\n\n"
        f"Journal tail:\n{owui.get_journal()}"
    )


# ---------------------------------------------------------------------------
# Post-upgrade checks
# ---------------------------------------------------------------------------
def test_login_after_upgrade(client, state):
    """Fresh signin on the upgraded build (does not reuse the pre-upgrade JWT).

    The admin account persists across the refresh, so signup would fail here.
    A fresh login exercises the real auth flow on the upgraded build and avoids
    false failures if token signing changed across the upgrade.
    """
    resp, token = owui.login_admin(client, client.base_url)
    assert resp.status_code == 200, (
        f"Admin signin returned {resp.status_code}: {resp.text}"
    )
    assert token, f"No token in signin response: {resp.text}"
    _set_bearer(client, token)
    state["token"] = token


def test_version_after_upgrade(client, state):
    """The upgraded build reports the version pinned in requirements.txt."""
    assert state.get("token"), "Login must succeed before checking version"
    pinned = owui.read_pinned_version()
    assert pinned, "Could not read pinned open-webui version from requirements.txt"

    r = client.get(f"{client.base_url}/api/config", timeout=QUICK_TIMEOUT)
    assert r.status_code == 200, f"GET /api/config returned {r.status_code}: {r.text}"
    reported = r.json().get("version", "")
    assert reported == pinned, (
        f"Version mismatch after upgrade: snap reports '{reported}', "
        f"dependencies/requirements.txt pins '{pinned}'"
    )


def test_model_available_after_upgrade(client, state):
    """The gemma model is still registered after the upgrade."""
    model_id = owui.wait_for_gemma_model(client, client.base_url)
    assert model_id, (
        f"gemma model missing from /api/models after the upgrade "
        f"(waited {owui.MODELS_TIMEOUT}s)."
    )
    state["model_id"] = model_id


def test_connect_provider_after_upgrade(client, state):
    """Replace the temporary baseline config with the actual content import."""
    config = owui.get_openai_config(client)
    assert state["provider_url"] in config["OPENAI_API_BASE_URLS"], (
        "The baseline endpoint configuration did not survive the upgrade"
    )

    # Remove the temporary API entry so it cannot mask a broken importer.
    owui.update_openai_config(client, state["original_openai_config"])
    subprocess.run(
        ["sudo", "snap", "connect", "open-webui:provider", "gemma4:provider"],
        check=True,
    )
    model_id = owui.wait_for_gemma_model(client, client.base_url)
    assert model_id, "No gemma model appeared after connecting gemma4:provider"
    state["model_id"] = model_id

    config = owui.get_openai_config(client)
    urls = config["OPENAI_API_BASE_URLS"]
    assert urls.count(state["provider_url"]) == 1
    entry = config["OPENAI_API_CONFIGS"][str(urls.index(state["provider_url"]))]
    assert {"name": "snap"} in entry.get("tags", []), (
        "Gemma4 endpoint was not registered by the provider importer"
    )


def test_text_prompt_after_upgrade(client, state):
    """A text prompt still returns a non-empty reply after the upgrade."""
    content = _text_prompt(client, state["model_id"], "local:upgrade-post-text")
    assert content.strip(), "Text prompt returned an empty response after upgrade"
