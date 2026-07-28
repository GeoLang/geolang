# Tests for agents
"""The tool manifest and executor sibyl calls: geolang runs tools in-process."""
from fastapi.testclient import TestClient

from src.agents.agent_manager import load_external_tools
from src.api import server

client = TestClient(server.app)


def test_manifest_covers_every_tool_module():
    manifest = client.get("/tools").json()["tools"]

    assert {t["name"] for t in manifest} == {f.__name__ for f, _ in load_external_tools()}
    for tool in manifest:
        assert tool["description"], f"{tool['name']} has no docstring"
        assert tool["parameters"]["type"] == "object"


def test_executor_runs_a_tool_and_returns_a_string():
    response = client.post("/tools/list_outputs", json={"args": {}})

    assert response.status_code == 200
    result = response.json()["result"]
    assert isinstance(result, str) and result
    assert not result.startswith("❌")


def test_unknown_tool_is_404():
    assert client.post("/tools/nonexistent", json={"args": {}}).status_code == 404


def test_bad_args_come_back_as_a_tool_error():
    response = client.post("/tools/geocode_place", json={"args": {}})

    assert response.status_code == 200
    assert response.json()["result"].startswith("❌ Invalid arguments:")
