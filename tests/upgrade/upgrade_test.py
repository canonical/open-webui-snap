"""Upgrade scenario for the Open WebUI snap.

Driven by tests/upgrade/run.sh.  By the time
this module runs, the harness has already:

  * removed and purged open-webui,
  * installed open-webui from the baseline channel (OWUI_FROM_CHANNEL,
    default stable),
  * installed gemma4,
  * best-effort connected the open-webui:config <-> gemma4:open-webui content
    interface (a no-op on plugin-based baselines).

This suite is the **migration test** across the two ways open-webui registers a
local inference snap:

  * Older baselines register gemma4 via the content interface + a DB import.
  * The target build (and future baselines) auto-discovers gemma4 via the
    bundled inference-snaps plugin, with no interface or manual config.

Whichever mechanism the baseline uses, gemma4 must remain usable across the
refresh.  gemma4 itself stays installed and running throughout.  These tests run
**in file order** inside a single pytest session:

  1. baseline server healthy
  2. first-signup admin (fresh, purged DB)
  3. gemma model present on the baseline build
  4. text prompt works on the baseline build
  5. upgrade (refresh) to the provided snap file / channel
  6. fresh login on the upgraded build (old JWT is intentionally not reused)
  7. reported version matches dependencies/requirements.txt
  8. the bundled plugin is seeded and active on the migrated DB
  9. a plugin-provided gemma model is present after the upgrade
 10. text prompt still works after the upgrade

Note: when upgrading from an interface-based baseline, the old DB import may
leave an orphaned "snap"-tagged connection behind.  The target build does not
clean this up (see the upgrade caveat in README.md), so a duplicate gemma model
entry is tolerated here rather than asserted against.
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

    Snap preserves data across the refresh, and gemma4 keeps running, so the
    model stays auto-discoverable without any reconnect.
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


def test_plugin_seeded_after_upgrade(client, state):
    """The bundled plugin is seeded and active on the *migrated* database.

    Seeding runs against a database that already exists (and, on interface-based
    baselines, already carries a legacy "snap" connection), so this asserts that
    the migration path really does end up with an active plugin rather than
    relying on whatever the baseline left behind.
    """
    assert state.get("token"), "Login must succeed before checking the plugin"
    func = owui.wait_for_seeded_function(client, client.base_url)
    assert func is not None, (
        f"Bundled plugin '{owui.SNAP_PLUGIN_ID}' is not present and active in "
        f"/api/v1/functions/ within {owui.MODELS_TIMEOUT}s after the upgrade.\n\n"
        f"Journal tail:\n{owui.get_journal()}"
    )
    assert func.get("type") == "pipe", (
        f"Expected seeded plugin to be a 'pipe', got: {func.get('type')!r}"
    )


def test_model_available_after_upgrade(client, state):
    """A gemma model served *by the plugin* is registered after the upgrade.

    A legacy "snap"-tagged connection left behind by an interface-based baseline
    can keep serving its own gemma entry, so requiring the plugin's model id
    prefix is what proves the plugin took over.
    """
    model_id = owui.wait_for_gemma_model(
        client, client.base_url, prefix=owui.SNAP_PLUGIN_MODEL_PREFIX
    )
    assert model_id, (
        f"No gemma model with the plugin prefix "
        f"'{owui.SNAP_PLUGIN_MODEL_PREFIX}' appeared in /api/models after the "
        f"upgrade (waited {owui.MODELS_TIMEOUT}s)."
    )
    state["model_id"] = model_id


def test_text_prompt_after_upgrade(client, state):
    """A text prompt still returns a non-empty reply after the upgrade."""
    content = _text_prompt(client, state["model_id"], "local:upgrade-post-text")
    assert content.strip(), "Text prompt returned an empty response after upgrade"
