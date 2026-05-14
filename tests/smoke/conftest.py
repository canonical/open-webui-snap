import os
import pytest
import requests


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("OWUI_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def client(base_url):
    s = requests.Session()
    s.base_url = base_url
    return s
