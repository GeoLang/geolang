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
