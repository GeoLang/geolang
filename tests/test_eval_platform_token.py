"""The bearer token the evals present to sibyl."""

import time

import jwt
import pytest

from evals.platform_token import SECRET_ENV, TOKEN_ENV, auth_headers, bearer_token
from evals.runner import active_session_id


@pytest.fixture(autouse=True)
def no_token_environment(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(SECRET_ENV, raising=False)


def test_a_stack_with_auth_off_needs_no_header():
    assert bearer_token() == ""
    assert auth_headers() == {}


def test_a_token_given_outright_is_presented(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "  handed.to.us  ")

    assert auth_headers() == {"Authorization": "Bearer handed.to.us"}


def test_the_secret_mints_a_token_every_platform_service_decodes(monkeypatch):
    secret = "a" * 40
    monkeypatch.setenv(SECRET_ENV, secret)

    claims = jwt.decode(bearer_token(), secret, algorithms=["HS256"])

    assert claims["sub"] == "geolang-evals"
    assert claims["exp"] > time.time()
    # sibyl validates with the platform default, which refuses an aud, and
    # refuses a token minted for another service's door
    assert "aud" not in claims
    assert not {"token_use", "geolang_use", "agora_use"} & set(claims)


def test_a_given_token_wins_over_the_secret(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "handed.to.us")
    monkeypatch.setenv(SECRET_ENV, "a" * 40)

    assert bearer_token() == "handed.to.us"


def test_a_refusal_is_not_read_as_a_session_list(monkeypatch, respx_mock):
    """sibyl answers 401 with an object, and iterating that walks its keys."""
    import httpx

    from evals import runner

    respx_mock.get(f"{runner.SIBYL}/sessions").mock(
        return_value=httpx.Response(401, json={"error": "missing bearer token"})
    )

    assert active_session_id() is None


def test_the_run_route_takes_the_token_in_the_body(monkeypatch):
    """/runs reads `user_token` as a field and never reads a header."""
    from evals.platform_token import run_body

    monkeypatch.setenv(TOKEN_ENV, "handed.to.us")

    assert run_body({"message": "hi"}) == {"message": "hi", "user_token": "handed.to.us"}


def test_a_stack_with_auth_off_sends_no_user_token():
    from evals.platform_token import run_body

    assert run_body({"message": "hi"}) == {"message": "hi"}
