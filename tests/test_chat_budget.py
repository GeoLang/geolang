import json
from datetime import date, timedelta

import pytest
import respx
from fastapi.testclient import TestClient

from src.api import server
from src.api.chat_budget import (
    CALLER_BUDGET_SPENT_REPLY,
    CHAT_RUNS_PER_CALLER_PER_DAY_ENV,
    CHAT_RUNS_PER_DAY_ENV,
    DAILY_BUDGET_SPENT_REPLY,
    ChatRunBudget,
)
from src.core.auth import SECRET_ENV
from tests.test_agui import _sse_data_objects
from tests.test_route_auth import SECRET, mint

client = TestClient(server.app)

AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "messages": [{"id": "m1", "role": "user", "content": "hi"}],
    "state": {},
    "tools": [],
    "context": [],
    "forwardedProps": {},
}

FIRST_DAY = date(2026, 9, 23)


class Clock:
    def __init__(self):
        self.day = FIRST_DAY

    def today(self):
        return self.day


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def budget(monkeypatch, clock):
    def install(runs_per_day, runs_per_caller_per_day):
        monkeypatch.setattr(
            server,
            "chat_run_budget",
            ChatRunBudget(runs_per_day, runs_per_caller_per_day, today=clock.today),
        )

    return install


@pytest.fixture
def sibyl():
    with respx.mock(base_url=server.SIBYL_URL) as mock:
        yield mock.post("/runs").respond(
            200, content=json.dumps({"kind": "done"}) + "\n"
        )


def chat(subject=None):
    headers = {"Authorization": f"Bearer {mint(sub=subject)}"} if subject else {}
    response = client.post("/chat/agui", json=AGUI_BODY, headers=headers)
    assert response.status_code == 200
    return _sse_data_objects(response.text.split("\n\n"))


def assert_refused_with(events, reply):
    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert events[1]["role"] == "assistant"
    assert events[2]["delta"] == reply


def test_a_capped_caller_is_refused_while_another_still_runs(gated, budget, sibyl):
    budget(None, 1)

    assert chat("u1")[-1]["type"] == "RUN_FINISHED"
    assert sibyl.call_count == 1

    assert_refused_with(chat("u1"), CALLER_BUDGET_SPENT_REPLY)
    assert sibyl.call_count == 1

    chat("u2")
    assert sibyl.call_count == 2


def test_the_global_cap_trips_across_callers(gated, budget, sibyl):
    budget(2, 5)

    chat("u1")
    chat("u2")
    assert_refused_with(chat("u3"), DAILY_BUDGET_SPENT_REPLY)
    assert sibyl.call_count == 2


def test_the_ungated_stack_is_held_to_the_global_cap(monkeypatch, budget, sibyl):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    budget(2, 1)

    chat()
    chat()
    assert_refused_with(chat(), DAILY_BUDGET_SPENT_REPLY)
    assert sibyl.call_count == 2


def test_a_new_utc_day_resets_both_counts(gated, budget, clock, sibyl):
    budget(2, 1)

    chat("u1")
    chat("u2")
    assert_refused_with(chat("u1"), DAILY_BUDGET_SPENT_REPLY)

    clock.day = FIRST_DAY + timedelta(days=1)
    chat("u1")
    chat("u2")
    assert sibyl.call_count == 4


@pytest.mark.parametrize("value", [None, "0", ""])
def test_unset_or_zero_means_no_limit(monkeypatch, gated, sibyl, value):
    for name in (CHAT_RUNS_PER_DAY_ENV, CHAT_RUNS_PER_CALLER_PER_DAY_ENV):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(server, "chat_run_budget", ChatRunBudget.from_environment())

    for _ in range(3):
        chat("u1")
    assert sibyl.call_count == 3


@pytest.mark.parametrize("name", [CHAT_RUNS_PER_DAY_ENV, CHAT_RUNS_PER_CALLER_PER_DAY_ENV])
@pytest.mark.parametrize("value", ["-1", "ten", "2.5"])
def test_an_invalid_limit_fails_startup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError) as raised:
        ChatRunBudget.from_environment()

    assert name in str(raised.value)
