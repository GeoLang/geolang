"""What the service refuses to start as.

A missing secret used to mean an open API. Now it is a startup error unless the
deployment opted out in writing, and the same opt-out is what keeps the
service-account fallback and the CORS wildcard from ever reaching a gated
deployment.
"""

import pytest

from src.api.server import CORS_ORIGINS_ENV, cors_origins
from src.core.auth import (
    SECRET_ENV,
    UNAUTHENTICATED_ENV,
    authentication_disabled,
    require_configuration,
)
from src.core.user_token import service_headers, user_token_scope

SECRET = "test-platform-secret-0123456789ab"
SERVICE_TOKEN_ENV = "PTOLEMY_API_TOKEN"
ORIGIN = "https://viewer.example.com"


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    monkeypatch.delenv(UNAUTHENTICATED_ENV, raising=False)


# ── the gate cannot be switched off by forgetting ────────────────────────


def test_no_secret_and_no_opt_out_refuses_to_start(unset):
    with pytest.raises(RuntimeError) as raised:
        require_configuration()

    assert SECRET_ENV in str(raised.value)
    assert UNAUTHENTICATED_ENV in str(raised.value)


def test_a_secret_is_enough_to_start(unset, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)

    require_configuration()
    assert not authentication_disabled()


def test_the_opt_out_is_enough_to_start(unset, monkeypatch):
    monkeypatch.setenv(UNAUTHENTICATED_ENV, "1")

    require_configuration()
    assert authentication_disabled()


@pytest.mark.parametrize("value", ["0", "no", "", "false"])
def test_only_a_real_yes_counts_as_opting_out(unset, monkeypatch, value):
    monkeypatch.setenv(UNAUTHENTICATED_ENV, value)

    with pytest.raises(RuntimeError):
        require_configuration()


# ── the service account cannot stand in for a caller ─────────────────────


def test_a_gated_call_with_no_caller_stays_anonymous(unset, monkeypatch):
    """With the gate on there is always a caller, so a fallback would be a bypass."""
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv(SERVICE_TOKEN_ENV, "service-account-token")

    with user_token_scope(None):
        assert service_headers(SERVICE_TOKEN_ENV) == {}


def test_the_fallback_still_serves_the_authless_stack(unset, monkeypatch):
    monkeypatch.setenv(UNAUTHENTICATED_ENV, "1")
    monkeypatch.setenv(SERVICE_TOKEN_ENV, "service-account-token")

    with user_token_scope(None):
        assert service_headers(SERVICE_TOKEN_ENV) == {
            "Authorization": "Bearer service-account-token"
        }


def test_the_caller_still_wins_over_the_fallback(unset, monkeypatch):
    monkeypatch.setenv(UNAUTHENTICATED_ENV, "1")
    monkeypatch.setenv(SERVICE_TOKEN_ENV, "service-account-token")

    with user_token_scope("callers-own-token"):
        assert service_headers(SERVICE_TOKEN_ENV) == {
            "Authorization": "Bearer callers-own-token"
        }


# ── cors ─────────────────────────────────────────────────────────────────


def test_a_gated_deployment_must_name_its_origins(unset, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)

    with pytest.raises(RuntimeError) as raised:
        cors_origins()

    assert CORS_ORIGINS_ENV in str(raised.value)


def test_a_gated_deployment_may_not_use_the_wildcard(unset, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv(CORS_ORIGINS_ENV, f"{ORIGIN},*")

    with pytest.raises(RuntimeError):
        cors_origins()


def test_named_origins_are_what_a_gated_deployment_gets(unset, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv(CORS_ORIGINS_ENV, f" {ORIGIN} , https://other.example.com ")

    assert cors_origins() == [ORIGIN, "https://other.example.com"]


def test_the_wildcard_survives_only_in_the_authless_stack(unset, monkeypatch):
    monkeypatch.setenv(UNAUTHENTICATED_ENV, "1")
    monkeypatch.delenv(CORS_ORIGINS_ENV, raising=False)

    assert cors_origins() == ["*"]
