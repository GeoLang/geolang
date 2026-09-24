"""The suite runs as the authless dev stack does, and says so.

Importing the app now refuses a deployment with no secret and no opt-out, so the
opt-out has to be set before any test imports it. `setdefault` so a run that
already chose a mode keeps it.

The executor app refuses to start without its shared secret in the same way. It
still runs no tools by itself: with `GEOLANG_EXECUTOR_URL` unset, every tool
call in the suite runs in the test process.
"""

import os

import pytest

from src.core import external_pacing
from src.core.auth import UNAUTHENTICATED_ENV
from src.core.place_lookup import GEOKODE_URL_ENV
from src.core.tool_executor import EXECUTOR_SECRET_ENV
from tests.geokode_fakes import GEOKODE_URL

os.environ.setdefault(UNAUTHENTICATED_ENV, "1")
os.environ.setdefault(EXECUTOR_SECRET_ENV, "test-executor-secret")
# a geocoder set in the shell would otherwise answer tests over the network
os.environ.pop(GEOKODE_URL_ENV, None)


@pytest.fixture
def geokode_env(monkeypatch, tmp_path):
    monkeypatch.setenv(GEOKODE_URL_ENV, GEOKODE_URL)
    monkeypatch.setattr(external_pacing, "PACING_DIRECTORY", tmp_path / "pacing")
