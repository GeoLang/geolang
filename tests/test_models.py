"""The model endpoints are thin proxies over sibyl, which owns model selection."""
import json

import httpx
import respx
from fastapi.testclient import TestClient

from src.api import server

client = TestClient(server.app)

PROFILES = {
    "active": "sonnet",
    "profiles": [
        {"id": "sonnet", "label": "Sonnet", "model": "sonnet-4", "available": True},
        {"id": "local", "label": "Local", "model": "qwen2.5", "available": False},
    ],
}


def test_models_forwards_sibyls_profiles():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/models").respond(200, json=PROFILES)

        response = client.get("/models")

    assert response.status_code == 200
    assert response.json() == PROFILES


def test_setting_the_model_forwards_the_body_and_the_204():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.put("/model").respond(204)

        response = client.put("/model", json={"id": "local"})

    assert response.status_code == 204
    assert json.loads(route.calls.last.request.content) == {"id": "local"}


def test_unknown_profile_stays_a_404():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.put("/model").respond(404, json={"detail": "no such profile"})

        response = client.put("/model", json={"id": "gone"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no such profile"}


def test_unavailable_profile_stays_a_409():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.put("/model").respond(409, json={"detail": "profile not available"})

        response = client.put("/model", json={"id": "local"})

    assert response.status_code == 409
    assert response.json() == {"detail": "profile not available"}


def test_sibyl_being_down_is_a_503():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.get("/models").mock(side_effect=httpx.ConnectError("refused"))
        sibyl.put("/model").mock(side_effect=httpx.ConnectError("refused"))

        listed = client.get("/models")
        switched = client.put("/model", json={"id": "local"})

    assert listed.status_code == 503
    assert "unreachable" in listed.json()["detail"]
    assert switched.status_code == 503


def test_setting_cloud_credentials_forwards_the_body_and_bearer():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.put("/model/cloud").respond(204)

        response = client.put(
            "/model/cloud",
            json={
                "base": "https://api.anthropic.com/v1",
                "key": "sk-ant-test",
                "models": "claude-sonnet-4-5",
            },
            headers={"Authorization": "Bearer user-token"},
        )

    assert response.status_code == 204
    assert json.loads(route.calls.last.request.content) == {
        "base": "https://api.anthropic.com/v1",
        "key": "sk-ant-test",
        "models": "claude-sonnet-4-5",
    }
    assert route.calls.last.request.headers["authorization"] == "Bearer user-token"


def test_invalid_cloud_credentials_stay_a_400():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        sibyl.put("/model/cloud").respond(400, json={"error": "base must be an http or https URL"})

        response = client.put("/model/cloud", json={"base": "not-a-url"})

    assert response.status_code == 400
    assert response.json() == {"error": "base must be an http or https URL"}


def test_upserting_a_provider_forwards_the_body_and_bearer():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.put("/model/providers").respond(204)

        response = client.put(
            "/model/providers",
            json={
                "id": "anthropic",
                "server": "cloud",
                "base": "https://api.anthropic.com/v1",
                "key": "sk-ant-test",
                "models": "claude-sonnet-4-5",
            },
            headers={"Authorization": "Bearer user-token"},
        )

    assert response.status_code == 204
    assert json.loads(route.calls.last.request.content)["id"] == "anthropic"
    assert route.calls.last.request.headers["authorization"] == "Bearer user-token"


def test_deleting_a_provider_forwards_the_id():
    with respx.mock(base_url=server.SIBYL_URL) as sibyl:
        route = sibyl.delete("/model/providers/anthropic").respond(204)

        response = client.delete(
            "/model/providers/anthropic",
            headers={"Authorization": "Bearer user-token"},
        )

    assert response.status_code == 204
    assert route.called
