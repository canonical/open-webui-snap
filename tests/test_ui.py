"""
Integration tests for the open-webui snap.

Test order matters — test_signup must run before the session-scoped
admin_page fixture is used by later tests, because the snap starts with
no accounts and the signup form is only shown once.
"""

import os
import re
import struct
import zlib
import pathlib
import urllib.request

import pytest
from playwright.sync_api import Page, expect, Browser, BrowserContext

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Test Admin")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "testadmin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "TestPassword123!")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_png(path: pathlib.Path) -> pathlib.Path:
    """Write a minimal 1×1 red PNG to *path* if it doesn't already exist."""
    if path.exists():
        return path
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xFF\x00\x00"))
    iend = chunk(b"IEND", b"")
    path.write_bytes(signature + ihdr + idat + iend)
    return path


def _make_pdf(path: pathlib.Path) -> pathlib.Path:
    """Write a minimal single-page PDF to *path* if it doesn't already exist."""
    if path.exists():
        return path
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"BT /F1 12 Tf 100 700 Td (Hello PDF) Tj ET\n"
        b"endstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n9\n%%EOF"
    )
    path.write_bytes(body)
    return path


def _make_audio(path: pathlib.Path) -> pathlib.Path:
    """Write a minimal WAV file (0.1 s silence) to *path*."""
    if path.exists():
        return path
    # 44-byte WAV header + 4410 bytes of silence (0.1 s @ 44100 Hz, mono, 16-bit)
    num_samples = 4410
    data_size = num_samples * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1, 1, 44100, 88200, 2, 16,
        b"data", data_size,
    )
    path.write_bytes(header + b"\x00" * data_size)
    return path


def _dismiss_any_modal(page: Page) -> None:
    """Close a release-notes or changelog modal if present."""
    close_btn = page.locator("button[aria-label='Close'], button:has-text('Close'), button:has-text('OK')")
    if close_btn.first.is_visible(timeout=3_000):
        close_btn.first.click()


def _open_model_selector(page: Page) -> None:
    """Click the model selector button."""
    page.locator(
        "button[aria-label='Select a model'], "
        "[data-testid='model-selector'], "
        "button:has-text('Select a model')"
    ).first.click()


def _select_gemma_model(page: Page) -> None:
    """Open model selector and pick the first gemma model found."""
    _open_model_selector(page)
    # Wait for the dropdown options to appear
    gemma_option = page.locator("[role='option']:has-text('gemma'), li:has-text('gemma')").first
    gemma_option.wait_for(timeout=15_000)
    gemma_option.click()


def _send_message(page: Page, text: str) -> None:
    chat_input = page.locator("textarea[placeholder], [data-testid='chat-input']").first
    chat_input.fill(text)
    page.keyboard.press("Enter")


def _wait_for_response(page: Page, timeout_ms: int = 120_000) -> None:
    """Wait until a new assistant message appears and streaming finishes."""
    # A "stop generation" button appears while streaming; wait for it to disappear.
    stop_btn = page.locator("button[aria-label='Stop generation'], button:has-text('Stop')")
    # It might take a moment to appear, then we wait for it to go away.
    try:
        stop_btn.first.wait_for(state="visible", timeout=10_000)
        stop_btn.first.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass  # Button may not appear in all versions; fall back to message check.

    # Ensure at least one assistant message is visible.
    expect(
        page.locator(".message.assistant, [data-role='assistant'], [data-testid='assistant-message']").first
    ).to_be_visible(timeout=timeout_ms)


# ── Test: server health ───────────────────────────────────────────────────────

def test_server_health_endpoint():
    """The /health endpoint must return HTTP 200."""
    response = urllib.request.urlopen(f"{BASE_URL}/health", timeout=10)
    assert response.status == 200, f"Unexpected status: {response.status}"


# ── Test: signup / first-run ──────────────────────────────────────────────────

def test_signup_or_login_form_shown(browser: Browser):
    """On a fresh install the signup form must be visible at the root URL."""
    context = browser.new_context(base_url=BASE_URL)
    page = context.new_page()
    page.goto("/")
    page.wait_for_load_state("networkidle")

    signup_visible = page.locator("input[name='name']").is_visible(timeout=8_000)
    login_visible = page.locator("input[name='email']").is_visible(timeout=3_000)
    assert signup_visible or login_visible, (
        "Expected signup form (first run) or login form, but neither was visible"
    )
    context.close()


# ── Tests requiring authentication ───────────────────────────────────────────
# All tests below receive the `admin_page` fixture from conftest.py.
# The first time it is used (test_release_notes or later) conftest will
# create the admin account and log in.

def test_release_notes_show_version(admin_page: Page):
    """A release-notes dialog should display a recognisable version string."""
    modal = admin_page.locator("dialog, [role='dialog'], .modal").first
    if modal.is_visible(timeout=6_000):
        text = modal.text_content() or ""
        assert re.search(r"\d+\.\d+\.\d+", text), (
            f"No semver version found in release-notes modal. Content:\n{text}"
        )
        _dismiss_any_modal(admin_page)
    else:
        pytest.skip("No release-notes modal appeared – may have been dismissed already")


def test_gemma4_model_appears_in_selector(admin_page: Page):
    """The gemma4 model (provided via snap connection) must appear in the model selector."""
    _dismiss_any_modal(admin_page)
    _open_model_selector(admin_page)
    gemma_entry = admin_page.locator("[role='option']:has-text('gemma'), li:has-text('gemma')").first
    expect(gemma_entry).to_be_visible(timeout=15_000)
    # Close selector
    admin_page.keyboard.press("Escape")


def test_text_prompt(admin_page: Page):
    """Send a plain text prompt and receive a response."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)
    _send_message(admin_page, "Reply with exactly one word: hello")
    _wait_for_response(admin_page, timeout_ms=120_000)


def test_image_upload_prompt(admin_page: Page):
    """Attach a PNG image and send a prompt about it."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)

    image_path = _make_png(pathlib.Path("/tmp/owui_test_image.png"))

    with admin_page.expect_file_chooser() as fc_info:
        admin_page.locator(
            "button[aria-label*='Attach'], button[aria-label*='Upload'], "
            "button[aria-label*='attach'], [data-testid='upload-button']"
        ).first.click()
    fc_info.value.set_files(str(image_path))

    _send_message(admin_page, "What colour is the dominant colour in this image? One word answer.")
    _wait_for_response(admin_page, timeout_ms=120_000)


def test_pdf_upload_prompt(admin_page: Page):
    """Attach a PDF document and send a prompt about it."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)

    pdf_path = _make_pdf(pathlib.Path("/tmp/owui_test.pdf"))

    with admin_page.expect_file_chooser() as fc_info:
        admin_page.locator(
            "button[aria-label*='Attach'], button[aria-label*='Upload'], "
            "button[aria-label*='attach'], [data-testid='upload-button']"
        ).first.click()
    fc_info.value.set_files(str(pdf_path))

    _send_message(admin_page, "Summarise this document in one sentence.")
    _wait_for_response(admin_page, timeout_ms=120_000)


def test_audio_upload_prompt(admin_page: Page):
    """Attach a WAV audio file and send a prompt."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)

    audio_path = _make_audio(pathlib.Path("/tmp/owui_test.wav"))

    with admin_page.expect_file_chooser() as fc_info:
        admin_page.locator(
            "button[aria-label*='Attach'], button[aria-label*='Upload'], "
            "button[aria-label*='attach'], [data-testid='upload-button']"
        ).first.click()
    fc_info.value.set_files(str(audio_path))

    _send_message(admin_page, "Describe this audio in one sentence.")
    _wait_for_response(admin_page, timeout_ms=120_000)

