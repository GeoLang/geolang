# Tests for agents
"""The tool manifest and executor sibyl calls: geolang runs tools in-process."""
import json

from fastapi.testclient import TestClient

from src.agents.agent_manager import approval_route_only, load_external_tools
from src.api import server

client = TestClient(server.app)

# the manifest goes out with every model turn, so prose in a docstring or a
# Field description is paid for on each one. Roughly 10% over what it takes today
MANIFEST_BYTE_CAP = 40_500


def test_manifest_covers_every_tool_module():
    manifest = client.get("/tools").json()["tools"]
    modules = {
        f.__name__ for f, _ in load_external_tools() if not approval_route_only(f)
    }

    assert {t["name"] for t in manifest} == modules
    for tool in manifest:
        assert tool["description"], f"{tool['name']} has no docstring"
        assert tool["parameters"]["type"] == "object"


def test_the_manifest_stays_under_the_byte_cap():
    size = len(json.dumps(server.tool_manifest()))

    assert size <= MANIFEST_BYTE_CAP, (
        f"the tool manifest is {size} bytes, over the {MANIFEST_BYTE_CAP} cap. "
        "Every model turn carries it, so trim a docstring or a Field description "
        "rather than raising the cap."
    )


def test_a_parameter_named_title_survives_schema_slimming():
    manifest = {t["name"]: t for t in server.tool_manifest()}

    assert "title" in manifest["plan_workflow"]["parameters"]["properties"]


def test_the_approval_route_is_the_one_tool_left_off_the_manifest():
    """It records the user pressing approve, so nothing the model can call has it."""
    left_off = {f.__name__ for f, _ in load_external_tools() if approval_route_only(f)}

    assert left_off == {"approve_workflow"}


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
