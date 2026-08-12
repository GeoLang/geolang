"""Where a tool call runs, and what the executor lets through.

Two halves: `src.core.tool_executor` decides between this process and a remote
executor, and `src.api.executor` is the remote one. The suite itself never sets
`GEOLANG_EXECUTOR_URL`, so every other test still runs its tools in-process.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api import executor
from src.core import tool_executor
from src.core.tool_executor import (
    EXECUTOR_SECRET_ENV,
    EXECUTOR_SECRET_HEADER,
    EXECUTOR_URL_ENV,
    execute_tool,
)
from src.core.user_token import current_user_token

SECRET = "test-executor-secret"
EXECUTOR_URL = "http://executor:8081"
TOKEN = "header.payload.signature"

client = TestClient(executor.app)


class NoArgs(BaseModel):
    pass


def tool_that_reports_its_caller():
    """Answer with the token this call is running as."""
    return f"ran as {current_user_token()}"


def tool_that_raises():
    """Fail the way a tool with a bad argument does."""
    raise ValueError("no such column")


@pytest.fixture
def one_tool(monkeypatch):
    """Register `tool_that_reports_its_caller` as the only tool the executor has."""
    monkeypatch.setattr(
        executor, "load_external_tools", lambda: [(tool_that_reports_its_caller, NoArgs)]
    )


@pytest.fixture
def remote(monkeypatch):
    """Point the dispatch at an executor and report the requests it makes."""
    monkeypatch.setenv(EXECUTOR_URL_ENV, EXECUTOR_URL)
    monkeypatch.setenv(EXECUTOR_SECRET_ENV, SECRET)
    sent = []

    def post(url, json, headers, timeout):
        sent.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(200, json={"result": "ok"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(tool_executor.httpx, "post", post)
    return sent


# ── which process runs the tool ──────────────────────────────────────────


def test_without_an_executor_the_tool_runs_here(monkeypatch):
    monkeypatch.delenv(EXECUTOR_URL_ENV, raising=False)

    assert execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN) == (
        f"ran as {TOKEN}"
    )


def test_the_token_does_not_outlive_an_in_process_call(monkeypatch):
    monkeypatch.delenv(EXECUTOR_URL_ENV, raising=False)

    execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN)

    assert current_user_token() is None


def test_with_an_executor_the_tool_does_not_run_here(remote):
    ran = []

    def probe():
        """Record that this tool ran in the calling process."""
        ran.append(True)
        return "local"

    assert execute_tool("probe", probe, {"a": 1}, TOKEN) == "ok"
    assert ran == []
    assert sent_once(remote)["url"] == f"{EXECUTOR_URL}/run/probe"
    assert sent_once(remote)["json"] == {"args": {"a": 1}}


def sent_once(sent):
    assert len(sent) == 1
    return sent[0]


def test_the_executor_call_carries_the_secret_and_the_caller(remote):
    execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN)

    headers = sent_once(remote)["headers"]
    assert headers[EXECUTOR_SECRET_HEADER] == SECRET
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_an_anonymous_call_carries_no_authorization(remote):
    execute_tool("probe", tool_that_reports_its_caller, {}, None)

    assert "Authorization" not in sent_once(remote)["headers"]


def test_a_tool_failure_in_the_executor_is_raised_here(monkeypatch, remote):
    def post(url, json, headers, timeout):
        return httpx.Response(
            200,
            json={"error": "no such column"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(tool_executor.httpx, "post", post)

    with pytest.raises(RuntimeError, match="no such column"):
        execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN)


def test_an_unreachable_executor_says_so(monkeypatch, remote):
    def post(url, json, headers, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(tool_executor.httpx, "post", post)

    with pytest.raises(RuntimeError, match="executor is unreachable"):
        execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN)


def test_a_refused_executor_call_is_not_reported_as_a_tool_result(monkeypatch, remote):
    def post(url, json, headers, timeout):
        return httpx.Response(401, json={"detail": "no"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(tool_executor.httpx, "post", post)

    with pytest.raises(RuntimeError, match="refused the call with 401"):
        execute_tool("probe", tool_that_reports_its_caller, {}, TOKEN)


# ── the executor's own gate ──────────────────────────────────────────────


def run(name="tool_that_reports_its_caller", secret=SECRET, token=None):
    headers = {}
    if secret is not None:
        headers[EXECUTOR_SECRET_HEADER] = secret
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(f"/run/{name}", json={"args": {}}, headers=headers)


def test_the_right_secret_runs_the_tool(one_tool):
    response = run()

    assert response.status_code == 200
    assert response.json()["result"] == "ran as None"


def test_no_secret_is_refused(one_tool):
    assert run(secret=None).status_code == 401


def test_a_wrong_secret_is_refused(one_tool):
    assert run(secret="not-the-secret").status_code == 401


def test_an_unknown_tool_is_still_401_without_the_secret():
    # the gate runs before the lookup, so a stranger cannot probe the catalogue
    assert run(name="nonexistent", secret=None).status_code == 401


def test_an_unknown_tool_is_404_with_it(one_tool):
    assert run(name="nonexistent").status_code == 404


def test_the_bearer_becomes_the_identity_the_tool_runs_as(one_tool):
    assert run(token=TOKEN).json()["result"] == f"ran as {TOKEN}"


def test_a_tool_that_raises_answers_with_the_reason(monkeypatch):
    monkeypatch.setattr(
        executor, "load_external_tools", lambda: [(tool_that_raises, NoArgs)]
    )

    response = run(name="tool_that_raises")

    assert response.status_code == 200
    assert response.json() == {"error": "no such column"}


def test_health_needs_no_secret():
    assert client.get("/health").status_code == 200


def test_it_refuses_to_start_without_a_secret(monkeypatch):
    monkeypatch.delenv(EXECUTOR_SECRET_ENV, raising=False)

    with pytest.raises(RuntimeError, match=EXECUTOR_SECRET_ENV):
        executor.require_configuration()
