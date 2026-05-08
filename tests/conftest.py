"""
Shared fixtures for Open WebUI snap integration tests.

Environment variables (set in CI workflow, or locally for dev):
  BASE_URL       - e.g. http://localhost:8080
  ADMIN_NAME     - display name for the first admin account
  ADMIN_EMAIL    - email address for the first admin account
  ADMIN_PASSWORD - password for the first admin account
"""

import os
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
    b = playwright_instance.chromium.launch(headless=True)
    yield b
    b.close()


# ── Admin-authenticated page (session-scoped so login only happens once) ──────

@pytest.fixture(scope="session")
def admin_context(browser: Browser):
    """Browser context that is logged in as the admin user.

    On the very first call this creates the admin account if the signup form
    is shown (i.e. a fresh install).
    """
    context = browser.new_context(base_url=BASE_URL)
    page = context.new_page()
    page.goto("/")
    page.wait_for_load_state("networkidle")

    # Fresh install → signup form visible
    name_input = page.locator("input[name='name']")
    if name_input.is_visible(timeout=8_000):
        name_input.fill(ADMIN_NAME)
        page.locator("input[name='email']").fill(ADMIN_EMAIL)
        page.locator("input[name='password']").fill(ADMIN_PASSWORD)
        page.locator("button[type='submit']").click()
        page.wait_for_load_state("networkidle")

    # Already has an account → login form
    email_input = page.locator("input[name='email']")
    if email_input.is_visible(timeout=5_000):
        email_input.fill(ADMIN_EMAIL)
        page.locator("input[name='password']").fill(ADMIN_PASSWORD)
        page.locator("button[type='submit']").click()
        page.wait_for_load_state("networkidle")

    page.close()
    yield context
    context.close()


@pytest.fixture()
def admin_page(admin_context: BrowserContext):
    """Fresh page inside the authenticated admin context."""
    page = admin_context.new_page()
    page.goto("/")
    page.wait_for_load_state("networkidle")
    yield page
    page.close()

