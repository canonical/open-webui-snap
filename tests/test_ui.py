"""
Integration tests for the open-webui snap.

Test order matters — test_signup must run before the session-scoped
admin_page fixture is used by later tests, because the snap starts with
no accounts and the signup form is only shown once.
"""

import os
import re
import time
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
    # Build a structurally valid PDF with correct xref offsets.
    obj1 = b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n"
    obj2 = b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n"
    obj3 = b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]>>\nendobj\n"

    header = b"%PDF-1.4\n"
    off1 = len(header)
    off2 = off1 + len(obj1)
    off3 = off2 + len(obj2)
    body = header + obj1 + obj2 + obj3

    xref_offset = len(body)
    xref = (
        b"xref\n"
        b"0 4\n"
        b"0000000000 65535 f \n"
        + f"{off1:010d} 00000 n \n".encode()
        + f"{off2:010d} 00000 n \n".encode()
        + f"{off3:010d} 00000 n \n".encode()
    )
    trailer = (
        b"trailer\n<</Size 4 /Root 1 0 R>>\n"
        b"startxref\n"
        + str(xref_offset).encode() + b"\n"
        b"%%EOF\n"
    )
    path.write_bytes(body + xref + trailer)
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


def _wait_for_file_processing(page: Page, timeout_ms: int = 30_000) -> None:
    """Wait for any file processing spinners in the input area to disappear."""
    try:
        # Spinner appears while file is being processed (e.g. audio transcription)
        spinner = page.locator("form .spinner_ajPY, form [class*='spinner'], form svg[class*='spin']").first
        if spinner.is_visible(timeout=2_000):
            spinner.wait_for(state="hidden", timeout=timeout_ms)
    except Exception:
        pass  # No spinner found or already done
    # Also ensure network activity is idle
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


def _dismiss_any_modal(page: Page) -> None:
    """Close a release-notes or changelog modal if present."""
    close_btn = page.locator("button[aria-label='Close'], button:has-text('Close'), button:has-text('OK')")
    if close_btn.first.is_visible(timeout=3_000):
        close_btn.first.click()


def _open_model_selector(page: Page) -> None:
    """Click the model selector button."""
    page.locator(
        "button[aria-label^='Selected model'], "
        "button#model-selector-0-button, "
        "button[aria-label='Select a model'], "
        "[data-testid='model-selector']"
    ).first.click()


def _select_gemma_model(page: Page) -> None:
    """Open model selector and pick the first gemma model found."""
    # Retry opening the selector in case the model list was stale on first open.
    deadline = time.time() + 60
    while True:
        _open_model_selector(page)
        gemma_option = page.locator("[role='option']:has-text('gemma'), li:has-text('gemma')").first
        if gemma_option.is_visible(timeout=3_000):
            gemma_option.click()
            return
        page.keyboard.press("Escape")
        if time.time() >= deadline:
            raise TimeoutError("Gemma model did not appear in model selector within 60 s")
        page.wait_for_timeout(3_000)


def _send_message(page: Page, text: str) -> None:
    chat_input = page.locator("#chat-input, .tiptap.ProseMirror, textarea[placeholder], [data-testid='chat-input']").first
    chat_input.click()
    chat_input.type(text)
    page.keyboard.press("Enter")
    # Wait until the page navigates to a chat URL (message was actually submitted)
    try:
        page.wait_for_url("**/c/**", timeout=15_000)
    except Exception:
        # If already at a chat URL or navigation doesn't happen, continue
        pass


def _wait_for_response(page: Page, timeout_ms: int = 120_000) -> None:
    """Wait until a new assistant message appears and streaming finishes."""
    # First, ensure we're at a chat URL (message was sent)
    if "/c/" not in page.url:
        try:
            page.wait_for_url("**/c/**", timeout=15_000)
        except Exception:
            raise AssertionError(
                f"Page did not navigate to a chat URL after sending message. "
                f"Current URL: {page.url}"
            )

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
        page.locator(
            ".chat-assistant, .message.assistant, [data-role='assistant'], "
            "[data-testid='assistant-message'], [data-message-role='assistant']"
        ).first
    ).to_be_visible(timeout=timeout_ms)


# ── Test: server health ───────────────────────────────────────────────────────

def test_server_health_endpoint():
    """The /health endpoint must return HTTP 200."""
    response = urllib.request.urlopen(f"{BASE_URL}/health", timeout=10)
    assert response.status == 200, f"Unexpected status: {response.status}"


# ── Test: signup / first-run ──────────────────────────────────────────────────

def test_signup_or_login_form_shown(browser: Browser):
    """On a fresh install the signup form must be visible at the root URL."""
    context = browser.new_context(
        base_url=BASE_URL,
        viewport={"width": 1920, "height": 1080},
    )
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
    # The model list is fetched when the dropdown opens; if gemma isn't registered
    # yet, close and reopen to re-fetch until it appears (up to 60 s total).
    deadline = time.time() + 60
    found = False
    while time.time() < deadline:
        _open_model_selector(admin_page)
        gemma_entry = admin_page.locator("[role='option']:has-text('gemma'), li:has-text('gemma')").first
        if gemma_entry.is_visible(timeout=3_000):
            found = True
            break
        # Close dropdown and wait before trying again
        admin_page.keyboard.press("Escape")
        admin_page.wait_for_timeout(3_000)
    if not found:
        pytest.fail("Gemma model did not appear in the model selector within 60 s")
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
        admin_page.locator("#input-menu-button").click()
        admin_page.locator("button:has-text('Upload Files')").first.click()
    fc_info.value.set_files(str(image_path))
    _wait_for_file_processing(admin_page)

    _send_message(admin_page, "What colour is the dominant colour in this image? One word answer.")
    _wait_for_response(admin_page, timeout_ms=120_000)


def test_pdf_upload_prompt(admin_page: Page):
    """Attach a PDF document and send a prompt about it."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)

    pdf_path = _make_pdf(pathlib.Path("/tmp/owui_test.pdf"))

    with admin_page.expect_file_chooser() as fc_info:
        admin_page.locator("#input-menu-button").click()
        admin_page.locator("button:has-text('Upload Files')").first.click()
    fc_info.value.set_files(str(pdf_path))
    _wait_for_file_processing(admin_page)

    _send_message(admin_page, "Summarise this document in one sentence.")
    _wait_for_response(admin_page, timeout_ms=120_000)


def test_audio_upload_prompt(admin_page: Page):
    """Attach a WAV audio file and send a prompt."""
    _dismiss_any_modal(admin_page)
    _select_gemma_model(admin_page)

    audio_path = _make_audio(pathlib.Path("/tmp/owui_test.wav"))

    with admin_page.expect_file_chooser() as fc_info:
        admin_page.locator("#input-menu-button").click()
        admin_page.locator("button:has-text('Upload Files')").first.click()
    fc_info.value.set_files(str(audio_path))
    _wait_for_file_processing(admin_page)

    _send_message(admin_page, "Describe this audio in one sentence.")
    _wait_for_response(admin_page, timeout_ms=120_000)

