"""
Shared fixtures for Open WebUI snap integration tests.
Environment variables (set in CI workflow, or locally for dev):
  BASE_URL       - e.g. http://localhost:8080
  ADMIN_NAME     - display name for the first admin account
  ADMIN_EMAIL    - email address for the first admin account
  ADMIN_PASSWORD - password for the first admin account
"""
import json
import os
import urllib.error
import urllib.request
import pytest
from playwright.sync_api import Page, BrowserContext, Browser, Playwright, sync_playwright
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Test Admin")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "testadmin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "TestPassword123!")
# ── Low-level browser fixtures ────────────────────────────────────────────────
@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as p:
        yield p
@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright):
    executable_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or None
    b = playwright_instance.chromium.launch(
        headless=True,
        executable_path=executable_path,
        args=["--window-size=1920,1080"],
    )
    yield b
    b.close()
# ── Auth helpers ──────────────────────────────────────────────────────────────
def _api_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())
def _signin() -> str:
    result = _api_post("/api/v1/auths/signin", {
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
    })
    return result["token"]
def _get_token() -> str:
    try:
        return _signin()
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            _api_post("/api/v1/auths/signup", {
                "name": ADMIN_NAME,
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
            })
            return _signin()
        raise
def _inject_token(page: Page, token: str) -> None:
    page.goto("/")
    page.evaluate(f"localStorage.setItem(\'token\', \'{token}\')")
    page.goto("/")
    page.wait_for_load_state("networkidle")
# ── Admin-authenticated page (session-scoped so login only happens once) ──────
@pytest.fixture(scope="session")
def admin_context(browser: Browser):
    token = _get_token()
    context = browser.new_context(
        base_url=BASE_URL,
        viewport={"width": 1920, "height": 1080},
    )
    page = context.new_page()
    _inject_token(page, token)
    page.close()
    yield context
    context.close()
@pytest.fixture()
def admin_page(admin_context: BrowserContext):
    page = admin_context.new_page()
    page.set_viewport_size({"width": 1920, "height": 1080})
    page.goto("/")
    page.wait_for_load_state("networkidle")
    yield page
    page.close()
