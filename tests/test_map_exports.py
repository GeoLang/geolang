import asyncio
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api import map_exports, server
from src.api.map_exports import (
    CALLER_EXPORT_RUNNING_REPLY,
    EXPORTS_BUSY_REPLY,
    MAXIMUM_CONCURRENT_EXPORTS,
    MAXIMUM_EXPORT_SIDE_PIXELS,
    ExportSlots,
)
from src.core import utils
from src.core.auth import SECRET_ENV
from tests.test_route_auth import SECRET, mint

client = TestClient(server.app)

EXPORT_PATHS = ["/export-png", "/export-pdf"]
SHORT_TIMEOUT_SECONDS = 0.05
WAIT_FOR_LOADING_SECONDS = 5


class FakePage:
    def __init__(self, chromium):
        self.chromium = chromium

    async def goto(self, url, **kwargs):
        await self.chromium.page_load(url)

    async def wait_for_timeout(self, milliseconds):
        return None

    async def pdf(self, **kwargs):
        self.chromium.written.append(kwargs["path"])

    async def query_selector(self, selector):
        return None

    async def screenshot(self, **kwargs):
        self.chromium.written.append(kwargs["path"])


class FakeBrowser:
    def __init__(self, chromium):
        self.chromium = chromium
        self.closed = False

    async def new_page(self, viewport):
        self.chromium.viewports.append(viewport)
        return FakePage(self.chromium)

    async def close(self):
        await self.chromium.browser_close()
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.viewports = []
        self.browsers = []
        self.written = []
        self.drivers_stopped = 0
        self.page_load = self.returns_at_once
        self.browser_close = self.returns_at_once

    async def returns_at_once(self, *args):
        return None

    async def launch(self, headless):
        browser = FakeBrowser(self)
        self.browsers.append(browser)
        return browser

    @asynccontextmanager
    async def async_playwright(self):
        try:
            yield SimpleNamespace(chromium=self)
        finally:
            self.drivers_stopped += 1


async def hangs(*args):
    await asyncio.Event().wait()


@pytest.fixture
def chromium(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(map_exports, "export_slots", ExportSlots(MAXIMUM_CONCURRENT_EXPORTS))
    fake = FakeChromium()
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=fake.async_playwright),
    )
    return fake


def signed_in(subject):
    return {"Authorization": f"Bearer {mint(sub=subject)}"}


@pytest.mark.parametrize("path", EXPORT_PATHS)
@pytest.mark.parametrize(
    "size",
    [
        {"width": MAXIMUM_EXPORT_SIDE_PIXELS + 1},
        {"height": MAXIMUM_EXPORT_SIDE_PIXELS + 1},
        {"width": 30000, "height": 30000},
        {"width": 0},
        {"height": -5},
    ],
)
def test_a_size_outside_the_caps_is_refused_before_chromium_starts(chromium, path, size):
    response = client.post(path, json=size, headers=signed_in("alice"))

    assert response.status_code == 422
    assert chromium.browsers == []


@pytest.mark.parametrize("path", EXPORT_PATHS)
@pytest.mark.parametrize("width, height", [(3840, 2160), (2160, 3840)])
def test_a_4k_export_fits_the_caps(chromium, path, width, height):
    response = client.post(path, json={"width": width, "height": height}, headers=signed_in("alice"))

    assert response.status_code == 200
    assert chromium.viewports == [{"width": width, "height": height}]
    assert chromium.browsers[0].closed
    assert chromium.drivers_stopped == 1


def test_a_caller_runs_one_export_at_a_time_within_a_global_limit(chromium):
    assert MAXIMUM_CONCURRENT_EXPORTS == 2

    async def scenario():
        release = asyncio.Event()
        both_loading = asyncio.Event()
        loading = []

        async def held(url):
            loading.append(url)
            if len(loading) == MAXIMUM_CONCURRENT_EXPORTS:
                both_loading.set()
            await release.wait()

        chromium.page_load = held
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            alice_first = asyncio.create_task(
                http.post("/export-png", json={}, headers=signed_in("alice"))
            )
            bob_first = asyncio.create_task(
                http.post("/export-pdf", json={}, headers=signed_in("bob"))
            )
            await asyncio.wait_for(both_loading.wait(), WAIT_FOR_LOADING_SECONDS)
            alice_second = await http.post("/export-pdf", json={}, headers=signed_in("alice"))
            carol_first = await http.post("/export-png", json={}, headers=signed_in("carol"))
            release.set()
            finished = [await alice_first, await bob_first]
            alice_after = await http.post("/export-png", json={}, headers=signed_in("alice"))
        return alice_second, carol_first, finished, alice_after

    alice_second, carol_first, finished, alice_after = asyncio.run(scenario())

    assert alice_second.status_code == 429
    assert alice_second.json()["detail"] == CALLER_EXPORT_RUNNING_REPLY
    assert carol_first.status_code == 429
    assert carol_first.json()["detail"] == EXPORTS_BUSY_REPLY
    assert [response.status_code for response in finished] == [200, 200]
    assert alice_after.status_code == 200
    assert len(chromium.browsers) == MAXIMUM_CONCURRENT_EXPORTS + 1


def test_a_chromium_that_fails_is_closed_and_frees_the_callers_slot(chromium):
    async def crashes(url):
        raise RuntimeError("chromium crashed")

    chromium.page_load = crashes
    failed = client.post("/export-png", json={}, headers=signed_in("alice"))
    chromium.page_load = chromium.returns_at_once
    retried = client.post("/export-png", json={}, headers=signed_in("alice"))

    assert failed.status_code == 500
    assert "chromium crashed" in failed.json()["detail"]
    assert chromium.browsers[0].closed
    assert chromium.drivers_stopped == 2
    assert retried.status_code == 200


def test_a_chromium_that_hangs_is_stopped_closed_and_frees_the_callers_slot(
    chromium, monkeypatch
):
    monkeypatch.setattr(map_exports, "EXPORT_TIMEOUT_SECONDS", SHORT_TIMEOUT_SECONDS)
    chromium.page_load = hangs

    hung = client.post("/export-pdf", json={}, headers=signed_in("alice"))
    chromium.page_load = chromium.returns_at_once
    retried = client.post("/export-pdf", json={}, headers=signed_in("alice"))

    assert hung.status_code == 504
    assert "was stopped" in hung.json()["detail"]
    assert chromium.browsers[0].closed
    assert chromium.drivers_stopped == 2
    assert retried.status_code == 200


def test_a_chromium_that_will_not_close_is_left_to_the_driver_exit(chromium, monkeypatch):
    monkeypatch.setattr(map_exports, "BROWSER_CLOSE_TIMEOUT_SECONDS", SHORT_TIMEOUT_SECONDS)
    chromium.browser_close = hangs

    response = client.post("/export-png", json={}, headers=signed_in("alice"))

    assert response.status_code == 200
    assert not chromium.browsers[0].closed
    assert chromium.drivers_stopped == 1
    assert map_exports.export_slots.running_callers == set()
