import multiprocessing
import threading
import time

import osmnx
import pytest
import requests

from src.core import external_pacing, utils
from src.core.external_pacing import (
    SECONDS_BETWEEN_REQUESTS,
    PacedRequests,
    wait_for_turn,
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
CALLERS = 3
START_WAIT_SECONDS = 60


@pytest.fixture
def pacing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(external_pacing, "PACING_DIRECTORY", tmp_path)
    return tmp_path


def take_turn_in_another_process(pacing_directory, ready, start, finished_at):
    external_pacing.PACING_DIRECTORY = pacing_directory
    ready.set()
    start.wait(START_WAIT_SECONDS)
    wait_for_turn(NOMINATIM_URL)
    finished_at.value = time.time()


def test_calls_from_two_processes_are_spaced_apart(pacing_directory):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    start = context.Event()
    finished_at = context.Value("d", 0.0)
    other = context.Process(
        target=take_turn_in_another_process,
        args=(pacing_directory, ready, start, finished_at),
    )
    other.start()
    assert ready.wait(START_WAIT_SECONDS)

    started_at = time.time()
    start.set()
    wait_for_turn(NOMINATIM_URL)
    ours_finished_at = time.time()
    other.join(START_WAIT_SECONDS)

    assert other.exitcode == 0
    later = max(ours_finished_at, finished_at.value)
    assert later - started_at >= SECONDS_BETWEEN_REQUESTS


def test_calls_from_threads_line_up_one_interval_apart(pacing_directory):
    started_at = time.time()
    callers = [
        threading.Thread(target=wait_for_turn, args=(NOMINATIM_URL,))
        for _ in range(CALLERS)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()

    assert time.time() - started_at >= (CALLERS - 1) * SECONDS_BETWEEN_REQUESTS


def test_osmnx_requests_wait_their_turn_once_the_geo_stack_is_loaded(
    pacing_directory, monkeypatch
):
    monkeypatch.setattr(osmnx.settings, "requests_kwargs", {})

    utils.preload_geo_stack()
    paced = osmnx.settings.requests_kwargs["auth"]
    request = requests.Request("GET", NOMINATIM_URL).prepare()
    started_at = time.time()
    paced(request)
    paced(request)

    assert isinstance(paced, PacedRequests)
    assert time.time() - started_at >= SECONDS_BETWEEN_REQUESTS
