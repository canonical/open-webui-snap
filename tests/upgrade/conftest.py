import os
import sys

import pytest
import requests

# Make the shared helper module (tests/shared/owui.py) importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("OWUI_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def client(base_url):
    s = requests.Session()
    s.base_url = base_url
    return s


@pytest.fixture(scope="session")
def state():
    """Mutable per-session bag shared across the ordered upgrade tests.

    Holds values produced by earlier tests (e.g. the gemma model id and the
    post-upgrade auth token) so later tests can consume them.
    """
    return {}
