"""The file-serving routes must not leave the tree.

`/outputs/{filename}`, `/geojson/{filename:path}`, `/stats/{filename:path}` and
`/live-data/{token}` all take a name from the URL and look it up in a list of
directories. Every lookup goes through `resolve_under`, which resolves both
sides and drops anything landing outside the allowed roots, so `..`, an absolute
name and a symlink out of the tree all read as "not found".

Outputs are one directory per caller, so a name is looked up in the directory of
whoever presented the bearer, and never in the parent that holds all of them.

The routes read `OUTPUTS_ROOT` and friends at call time, so these tests point
`TOOL_EXEC_DIR` at a tmp_path and reload the modules that cached them.
"""

import asyncio
import importlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core import utils
from src.core.auth import SECRET_ENV
from src.core.user_token import user_token_scope


@pytest.fixture
def tree(monkeypatch, tmp_path):
    """A throwaway exec dir, plus a secret next door that must stay unreachable."""
    exec_dir = tmp_path / "exec"
    (exec_dir / "outputs").mkdir(parents=True)
    (exec_dir / "user_data" / "nested").mkdir(parents=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not serve me")

    monkeypatch.setenv("TOOL_EXEC_DIR", str(exec_dir))
    importlib.reload(utils)
    server = importlib.reload(importlib.import_module("src.api.server"))

    yield server, exec_dir, outside

    # put the module-level dirs back for the rest of the suite
    monkeypatch.delenv("TOOL_EXEC_DIR", raising=False)
    importlib.reload(utils)
    importlib.reload(importlib.import_module("src.api.server"))


@pytest.fixture
def client(tree):
    server, _, _ = tree
    return TestClient(server.app)


SECRET = "test-platform-secret-0123456789ab"


def outputs_of(subject=None):
    """The outputs directory of `subject`, or of an anonymous caller."""
    token = token_for(subject) if subject else None
    with user_token_scope(token):
        return Path(utils.caller_outputs_dir())


def token_for(subject):
    """A live platform token for `subject`, signed the way ptolemy signs one."""
    return jwt.encode(
        {"sub": subject, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET,
        algorithm="HS256",
    )


def as_subject(subject):
    return {"Authorization": f"Bearer {token_for(subject)}"}


def status_of(coro):
    """The status the route answered a raw filename with.

    The routes are called directly rather than over the test client: httpx
    normalizes `..` out of a URL before sending, so a client-level traversal
    never reaches the app and would prove nothing about its own check. Starlette
    also hands a percent-decoded value to the route, so this is what a decoded
    `%2e%2e%2f` arrives as.
    """
    try:
        asyncio.run(coro)
    except HTTPException as e:
        return e.status_code
    return 200


def point(path, name="p"):
    """A one-point GeoJSON file geopandas can actually read."""
    path.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{"name":"%s"},'
        '"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}' % name
    )


# ── the helper ───────────────────────────────────────────────────────────


def test_resolve_under_finds_a_file_in_an_allowed_dir(tree):
    server, exec_dir, _ = tree
    target = exec_dir / "outputs" / "layer.gpkg"
    target.write_text("x")

    found = server.resolve_under(
        ["layer.gpkg"], [str(exec_dir / "outputs")], [str(exec_dir / "outputs")]
    )

    assert found == str(target.resolve())


def test_resolve_under_rejects_a_traversal(tree):
    server, exec_dir, outside = tree

    assert (
        server.resolve_under(
            ["../../outside/secret.txt"],
            [str(exec_dir / "outputs")],
            [str(exec_dir / "outputs")],
        )
        is None
    )


def test_resolve_under_rejects_an_absolute_name(tree):
    server, exec_dir, outside = tree
    # an absolute name swallows the search dir, so this is the real escape
    assert (
        server.resolve_under(
            [str(outside / "secret.txt")],
            [str(exec_dir / "outputs")],
            [str(exec_dir / "outputs")],
        )
        is None
    )


def test_resolve_under_rejects_a_symlink_out_of_the_tree(tree):
    server, exec_dir, outside = tree
    link = exec_dir / "outputs" / "escape.txt"
    link.symlink_to(outside / "secret.txt")

    # the link resolves outside, so confinement drops it even though it exists
    assert link.exists()
    assert (
        server.resolve_under(
            ["escape.txt"], [str(exec_dir / "outputs")], [str(exec_dir / "outputs")]
        )
        is None
    )


def test_resolve_under_keeps_the_gpkg_fallback(tree):
    server, exec_dir, _ = tree
    target = exec_dir / "outputs" / "roads.gpkg"
    target.write_text("x")

    # the viewer links to the bare name
    found = server.resolve_under(
        server.name_candidates("roads"),
        [str(exec_dir / "outputs")],
        [str(exec_dir / "outputs")],
    )

    assert found == str(target.resolve())


def test_the_user_data_subdirs_are_not_allowed_roots(tree):
    server, exec_dir, _ = tree
    roots = server.allowed_roots()

    # confinement is to the parents, so a symlinked subdir cannot widen it
    assert str(exec_dir / "user_data" / "nested") not in roots


def test_the_tree_root_is_neither_a_root_nor_searched(tree):
    server, exec_dir, _ = tree

    # it contains every caller's outputs directory, so allowing it allows those
    assert str(exec_dir) not in server.allowed_roots()
    assert str(exec_dir) not in server.layer_search_dirs()


def test_a_loose_file_at_the_tree_root_is_not_served(client, tree):
    _, exec_dir, _ = tree
    point(exec_dir / "stray.geojson")

    assert client.get("/geojson/stray.geojson").status_code == 404
    assert client.get("/stats/stray.geojson").status_code == 404


def test_a_natural_earth_set_is_served_whatever_it_is_called(client, tree):
    _, exec_dir, _ = tree
    # a set the search list never spelled out by name, reached by the glob
    directory = exec_dir / "natural_earth_110m_populated_places"
    directory.mkdir()
    point(directory / "ne_110m_populated_places.geojson", "london")

    assert client.get("/geojson/ne_110m_populated_places.geojson").status_code == 200
    assert client.get("/stats/ne_110m_populated_places.geojson").json()["count"] == 1


def test_a_symlinked_natural_earth_set_cannot_widen_the_boundary(tree):
    server, exec_dir, outside = tree
    (exec_dir / "natural_earth_evil").symlink_to(outside)

    assert str(exec_dir / "natural_earth_evil") not in server.allowed_roots()
    assert status_of(server.get_geojson("secret.txt")) == 404


# ── /outputs ─────────────────────────────────────────────────────────────


def test_outputs_serves_a_file(client, tree):
    _, exec_dir, _ = tree
    (outputs_of() / "report.txt").write_text("hello")

    response = client.get("/outputs/report.txt")

    assert response.status_code == 200
    assert response.text == "hello"


def test_outputs_refuses_a_traversal(tree):
    server, _, _ = tree

    assert status_of(server.get_output("../../outside/secret.txt")) == 404
    assert status_of(server.get_output("../" * 12 + "etc/passwd")) == 404


def test_outputs_refuses_an_absolute_path(tree):
    server, _, outside = tree

    assert status_of(server.get_output(str(outside / "secret.txt"))) == 404


def test_outputs_refuses_a_symlink_out_of_the_tree(tree):
    server, exec_dir, outside = tree
    (outputs_of() / "escape.txt").symlink_to(outside / "secret.txt")

    assert status_of(server.get_output("escape.txt")) == 404


def test_outputs_refuses_encoded_separators_end_to_end(client):
    # belt on top of the direct calls above: whether the encoded form is
    # normalized in transit or decoded into the param, nothing is served
    for attempt in (
        "%2e%2e%2f%2e%2e%2foutside%2fsecret.txt",
        "..%2F..%2Foutside%2Fsecret.txt",
        "%2Fetc%2Fpasswd",
    ):
        response = client.get(f"/outputs/{attempt}")
        assert response.status_code == 404, attempt
        assert "do not serve me" not in response.text


# ── one directory per caller ─────────────────────────────────────────────
#
# the gate is on for these: a subject only exists when the token verifies


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)


def test_outputs_serves_only_the_callers_own_file(client, gated):
    (outputs_of("alice") / "alice.txt").write_text("alice's")
    (outputs_of("bob") / "bob.txt").write_text("bob's")

    assert client.get("/outputs/alice.txt", headers=as_subject("alice")).text == "alice's"
    assert client.get("/outputs/alice.txt", headers=as_subject("bob")).status_code == 404
    assert client.get("/outputs/bob.txt", headers=as_subject("alice")).status_code == 404


def test_download_serves_only_the_callers_own_file(client, gated):
    (outputs_of("alice") / "alice.gpkg").write_text("alice's")

    assert client.get("/download/alice.gpkg", headers=as_subject("alice")).text == "alice's"
    assert client.get("/download/alice.gpkg", headers=as_subject("bob")).status_code == 404
    # the bare name the viewer links to must not find it either
    assert client.get("/download/alice", headers=as_subject("bob")).status_code == 404


def test_a_caller_cannot_climb_into_another_directory(gated, tree):
    # the routes are called directly: httpx normalizes `..` out of a url, so a
    # traversal sent through the client would never reach the check
    server, _, _ = tree
    (outputs_of("alice") / "alice.txt").write_text("alice's")
    alice_directory = outputs_of("alice").name
    bob = f"Bearer {token_for('bob')}"

    for attempt in (
        f"../{alice_directory}/alice.txt",
        f"../../outputs/{alice_directory}/alice.txt",
        str(outputs_of("alice") / "alice.txt"),
    ):
        assert status_of(server.get_output(attempt, bob)) == 404, attempt
        assert status_of(server.download_file(attempt, bob)) == 404, attempt


def test_a_subject_that_names_a_path_stays_in_the_outputs_root(gated, tree):
    _, exec_dir, _ = tree
    outputs_root = exec_dir / "outputs"

    for subject in ("../../escape", "..", "a/b/c", "/etc/passwd"):
        directory = outputs_of(subject)
        assert directory.parent == outputs_root, subject
        assert directory.exists()


def test_subjects_that_sanitize_alike_get_different_directories(gated, tree):
    # both sides sanitize to the same string, so only the digest keeps them apart
    assert outputs_of("a/b") != outputs_of("a:b")
    assert outputs_of("../x") != outputs_of("__/x")


def test_no_subject_can_reach_the_anonymous_directory(gated, tree):
    anonymous = outputs_of()

    for subject in (
        utils.ANONYMOUS_OUTPUTS_DIRECTORY,
        f"{utils.ANONYMOUS_OUTPUTS_DIRECTORY}/",
        f"/{utils.ANONYMOUS_OUTPUTS_DIRECTORY}",
        " ",
    ):
        assert outputs_of(subject) != anonymous, subject


def test_geojson_and_stats_refuse_another_callers_layer(client, gated, tree):
    point(outputs_of("alice") / "sites.geojson")
    # the path a caller would write to name a file in someone else's directory
    named = f"outputs/{outputs_of('alice').name}/sites.geojson"

    for route in ("/geojson", "/stats"):
        assert client.get(f"{route}/{named}", headers=as_subject("alice")).status_code == 200
        assert client.get(f"{route}/{named}", headers=as_subject("bob")).status_code == 404
        assert (
            client.get(f"{route}/sites.geojson", headers=as_subject("bob")).status_code
            == 404
        )


def test_an_anonymous_caller_cannot_read_a_subjects_file(client, gated):
    (outputs_of("alice") / "alice.txt").write_text("alice's")

    # the gate answers first, but the directory would not have matched either
    assert client.get("/outputs/alice.txt").status_code == 401
    assert outputs_of() != outputs_of("alice")


# ── /geojson ─────────────────────────────────────────────────────────────


def test_geojson_serves_a_file_from_outputs(client, tree):
    _, exec_dir, _ = tree
    point(outputs_of() / "depots.geojson", "depot")

    response = client.get("/geojson/depots.geojson")

    assert response.status_code == 200
    assert response.json()["features"][0]["properties"]["name"] == "depot"


def test_geojson_serves_a_file_from_a_user_data_subdir(client, tree):
    _, exec_dir, _ = tree
    point(exec_dir / "user_data" / "nested" / "parcels.geojson", "parcel")

    # the rglob search still reaches it, confinement to user_data allows it
    response = client.get("/geojson/parcels.geojson")

    assert response.status_code == 200
    assert response.json()["features"][0]["properties"]["name"] == "parcel"


def test_geojson_refuses_a_traversal(tree):
    server, _, _ = tree

    # :path keeps the separators, so this is the one that really escaped before
    assert status_of(server.get_geojson("../../outside/secret.txt")) == 404
    assert status_of(server.get_geojson("../" * 12 + "etc/passwd")) == 404


def test_geojson_refuses_an_absolute_path(tree):
    server, _, outside = tree

    assert status_of(server.get_geojson(str(outside / "secret.txt"))) == 404


def test_geojson_refuses_a_symlink_out_of_the_tree(tree):
    server, exec_dir, outside = tree
    (outputs_of() / "escape.geojson").symlink_to(outside / "secret.txt")

    assert status_of(server.get_geojson("escape.geojson")) == 404


def test_geojson_does_not_echo_the_reader_error(client, tree):
    _, exec_dir, _ = tree
    junk = outputs_of() / "junk.geojson"
    junk.write_text("SUPERSECRETCONTENT")

    response = client.get("/geojson/junk.geojson")

    assert response.status_code == 500
    # the reader quotes the absolute path, which maps out the container
    assert str(junk) not in response.text
    assert response.json()["detail"] == "Failed to convert to GeoJSON"


def test_geojson_does_not_echo_the_parse_position(client, tree):
    _, exec_dir, _ = tree
    # on a parse failure the reader names the offending character and its
    # offset, which is one byte of file content per request
    (outputs_of() / "bad.geojson").write_text(
        '{"type":"FeatureCollection","features":[ZLEAK]}'
    )

    response = client.get("/geojson/bad.geojson")

    assert response.status_code == 500
    assert "Unexpected character" not in response.text
    assert response.json()["detail"] == "Failed to convert to GeoJSON"


# ── /live-data ───────────────────────────────────────────────────────────
#
# the one route that serves a file without a platform token, so the token in the
# url is the whole credential and nothing else in the path may be honoured


def published(exec_dir, token, text='{"type":"FeatureCollection","features":[]}'):
    directory = exec_dir / "live_data"
    directory.mkdir(exist_ok=True)
    (directory / f"{token}.geojson").write_text(text)
    return token


TOKEN = "TAtDU_iGhkTfZlYyEezXgw0LrfjTKzL8hYbG1SUdWHo"


def test_live_data_serves_a_published_layer(client, tree):
    _, exec_dir, _ = tree
    published(exec_dir, TOKEN, '{"type":"FeatureCollection","features":[1]}')

    response = client.get(f"/live-data/{TOKEN}")

    assert response.status_code == 200
    assert response.json()["features"] == [1]


def test_live_data_needs_no_token_of_the_callers_own(client, tree, monkeypatch):
    """A share link guest in a live document never signs in."""
    monkeypatch.setenv(SECRET_ENV, SECRET)
    _, exec_dir, _ = tree
    published(exec_dir, TOKEN)

    assert client.get(f"/live-data/{TOKEN}").status_code == 200


def test_reading_a_published_layer_dates_it_as_used(client, tree):
    """A fetch is what keeps a file from expiring, so the read has to record it."""
    _, exec_dir, _ = tree
    published(exec_dir, TOKEN)
    path = exec_dir / "live_data" / f"{TOKEN}.geojson"
    long_ago = time.time() - 100 * 24 * 60 * 60
    os.utime(path, (long_ago, long_ago))

    assert client.get(f"/live-data/{TOKEN}").status_code == 200

    assert time.time() - path.stat().st_mtime < 60


def test_live_data_refuses_an_unpublished_token(client, tree):
    assert client.get(f"/live-data/{TOKEN}").status_code == 404


def test_live_data_refuses_anything_that_is_not_a_token(tree):
    server, exec_dir, outside = tree
    published(exec_dir, TOKEN)

    for attempt in (
        "../../outside/secret.txt",
        f"../live_data/{TOKEN}",
        str(outside / "secret.txt"),
        f"{TOKEN}.geojson",
        "short",
        "",
    ):
        assert status_of(server.get_live_data(attempt)) == 404, attempt


def test_live_data_refuses_a_symlink_out_of_the_tree(tree):
    server, exec_dir, outside = tree
    directory = exec_dir / "live_data"
    directory.mkdir(exist_ok=True)
    (directory / f"{TOKEN}.geojson").symlink_to(outside / "secret.txt")

    assert status_of(server.get_live_data(TOKEN)) == 404


# ── /stats ───────────────────────────────────────────────────────────────


def test_stats_reads_a_file_from_outputs(client, tree):
    _, exec_dir, _ = tree
    point(outputs_of() / "sites.geojson")

    response = client.get("/stats/sites.geojson")

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_stats_refuses_a_traversal(tree):
    server, _, _ = tree

    assert status_of(server.get_stats("../../outside/secret.txt")) == 404
    assert status_of(server.get_stats("../" * 12 + "etc/passwd")) == 404


def test_stats_refuses_an_absolute_path(tree):
    server, _, outside = tree

    assert status_of(server.get_stats(str(outside / "secret.txt"))) == 404


def test_stats_does_not_echo_the_reader_error(client, tree):
    _, exec_dir, _ = tree
    junk = outputs_of() / "junk.geojson"
    junk.write_text("SUPERSECRETCONTENT")

    response = client.get("/stats/junk.geojson")

    assert response.status_code == 500
    assert str(junk) not in response.text
    assert response.json()["detail"] == "Could not read the layer"
