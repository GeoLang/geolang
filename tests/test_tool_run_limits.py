import threading
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api import server
from src.api.tool_run_limits import (
    CALLER_OUTPUT_SPENT_REPLY,
    CALLER_RUNS_SPENT_REPLY,
    OUTPUT_MEGABYTES_PER_CALLER_PER_DAY_ENV,
    RUNS_AT_ONCE_REPLY,
    RUNS_SPENT_TODAY_REPLY,
    TOOL_RUNS_AT_ONCE_PER_CALLER_ENV,
    TOOL_RUNS_PER_CALLER_PER_DAY_ENV,
    TOOL_RUNS_PER_DAY_ENV,
    ToolRunLimits,
)
from src.api.upload_limits import BYTES_PER_MEGABYTE
from src.core import tool_executor, utils
from src.core.auth import SECRET_ENV
from tests.test_route_auth import SECRET, mint

client = TestClient(server.app)

FIRST_DAY = date(2026, 9, 23)
PROBE_WAIT_SECONDS = 10
OUTPUT_BUDGET_BYTES = 100


class Clock:
    def __init__(self):
        self.day = FIRST_DAY

    def today(self):
        return self.day


class ProbeArgs(BaseModel):
    written_bytes: int = 0


class Probe:
    def __init__(self):
        self.runs = 0
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, written_bytes: int = 0):
        self.runs += 1
        if written_bytes:
            path = f"{utils.caller_outputs_dir()}/probe_{self.runs}.bin"
            with open(path, "wb") as written:
                written.write(b"x" * written_bytes)
        if self.hold:
            self.hold = False
            self.entered.set()
            assert self.release.wait(PROBE_WAIT_SECONDS)
        return "ok"


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(tmp_path / "outputs"))
    tool = Probe()

    def probe(written_bytes: int = 0):
        return tool(written_bytes)

    monkeypatch.setattr(server, "load_external_tools", lambda: [(probe, ProbeArgs)])
    return tool


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def limits(monkeypatch, clock):
    def install(
        runs_per_day=None,
        runs_per_caller_per_day=None,
        runs_at_once_per_caller=None,
        output_bytes_per_caller_per_day=None,
    ):
        monkeypatch.setattr(
            tool_executor,
            "tool_runs",
            ToolRunLimits(
                runs_per_day,
                runs_per_caller_per_day,
                runs_at_once_per_caller,
                output_bytes_per_caller_per_day,
                today=clock.today,
            ),
        )

    return install


def run(subject, written_bytes=0):
    return client.post(
        "/tools/probe",
        json={"args": {"written_bytes": written_bytes}},
        headers={"Authorization": f"Bearer {mint(sub=subject)}"},
    )


def assert_refused_with(response, reply):
    assert response.status_code == 429
    assert response.json()["detail"] == reply


def test_a_capped_caller_gets_429_while_another_still_runs(probe, limits):
    limits(runs_per_caller_per_day=1)

    assert run("alice").status_code == 200
    assert_refused_with(run("alice"), CALLER_RUNS_SPENT_REPLY)
    assert run("bob").status_code == 200
    assert probe.runs == 2


def test_the_global_cap_trips_across_callers(probe, limits):
    limits(runs_per_day=2)

    assert run("alice").status_code == 200
    assert run("bob").status_code == 200
    assert_refused_with(run("carol"), RUNS_SPENT_TODAY_REPLY)
    assert probe.runs == 2


def test_a_new_utc_day_resets_the_counts(probe, limits, clock):
    limits(runs_per_caller_per_day=1)
    run("alice")
    assert run("alice").status_code == 429

    clock.day = FIRST_DAY + timedelta(days=1)

    assert run("alice").status_code == 200


def test_a_second_run_at_once_is_refused_until_the_first_finishes(probe, limits):
    limits(runs_at_once_per_caller=1, runs_per_caller_per_day=2)
    probe.hold = True
    first = {}
    holder = threading.Thread(target=lambda: first.update(response=run("alice")))
    holder.start()
    assert probe.entered.wait(PROBE_WAIT_SECONDS)

    refused = run("alice")
    other_caller = run("bob")
    probe.release.set()
    holder.join(PROBE_WAIT_SECONDS)

    assert_refused_with(refused, RUNS_AT_ONCE_REPLY.format(limit=1))
    assert other_caller.json()["result"] == "ok"
    assert first["response"].json()["result"] == "ok"
    # the refused call was not counted against the day
    assert run("alice").status_code == 200


def test_output_past_the_daily_budget_refuses_the_next_run(probe, limits):
    limits(output_bytes_per_caller_per_day=OUTPUT_BUDGET_BYTES)

    assert run("alice", written_bytes=OUTPUT_BUDGET_BYTES + 1).status_code == 200
    assert_refused_with(run("alice"), CALLER_OUTPUT_SPENT_REPLY)
    assert run("bob").status_code == 200


def test_files_already_in_the_outputs_are_not_charged_to_a_run(probe, limits):
    limits(output_bytes_per_caller_per_day=OUTPUT_BUDGET_BYTES)
    outputs = Path(utils.OUTPUTS_ROOT) / utils.caller_directory_name("alice")
    outputs.mkdir(parents=True)
    (outputs / "earlier.bin").write_bytes(b"x" * OUTPUT_BUDGET_BYTES * 3)

    assert run("alice", written_bytes=OUTPUT_BUDGET_BYTES // 2).status_code == 200
    assert run("alice").status_code == 200


def test_the_ungated_stack_is_held_to_the_global_cap_only(monkeypatch, probe, limits):
    monkeypatch.delenv(SECRET_ENV)
    limits(runs_per_caller_per_day=1, runs_per_day=2)

    first = client.post("/tools/probe", json={"args": {}})
    second = client.post("/tools/probe", json={"args": {}})
    third = client.post("/tools/probe", json={"args": {}})

    assert [first.status_code, second.status_code] == [200, 200]
    assert_refused_with(third, RUNS_SPENT_TODAY_REPLY)


LIMIT_VARIABLES = [
    TOOL_RUNS_PER_DAY_ENV,
    TOOL_RUNS_PER_CALLER_PER_DAY_ENV,
    TOOL_RUNS_AT_ONCE_PER_CALLER_ENV,
    OUTPUT_MEGABYTES_PER_CALLER_PER_DAY_ENV,
]


@pytest.mark.parametrize("value", ["", "0"])
def test_unset_or_zero_means_no_limit(monkeypatch, value):
    for name in LIMIT_VARIABLES:
        monkeypatch.setenv(name, value)

    limits = ToolRunLimits.from_environment()

    assert limits.run_budget.limit_per_day is None
    assert limits.run_budget.limit_per_caller_per_day is None
    assert limits.runs_at_once_per_caller is None
    assert limits.output_budget.limit_per_caller_per_day is None


def test_output_megabytes_are_read_as_megabytes(monkeypatch):
    monkeypatch.setenv(OUTPUT_MEGABYTES_PER_CALLER_PER_DAY_ENV, "500")

    limits = ToolRunLimits.from_environment()

    assert limits.output_budget.limit_per_caller_per_day == 500 * BYTES_PER_MEGABYTE


@pytest.mark.parametrize("name", LIMIT_VARIABLES)
def test_an_invalid_limit_fails_startup(monkeypatch, name):
    monkeypatch.setenv(name, "-3")

    with pytest.raises(RuntimeError, match=name):
        ToolRunLimits.from_environment()
