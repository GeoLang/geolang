import asyncio
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api import server
from src.api.upload_limits import (
    BYTES_PER_MEGABYTE,
    CALLER_BYTES_SPENT_REPLY,
    CALLER_FILES_SPENT_REPLY,
    UPLOAD_FILES_PER_CALLER_PER_DAY_ENV,
    UPLOAD_FILES_PER_DAY_ENV,
    UPLOAD_MAX_FILE_MEGABYTES_ENV,
    UPLOAD_MAX_REQUEST_MEGABYTES_ENV,
    UPLOAD_MAX_UNZIPPED_MEGABYTES_ENV,
    UPLOAD_MAX_ZIP_ENTRIES_ENV,
    UPLOAD_MEGABYTES_PER_CALLER_PER_DAY_ENV,
    UPLOAD_MEGABYTES_PER_DAY_ENV,
    UploadBudget,
    UploadLimits,
)
from src.core import utils
from src.core.auth import SECRET_ENV
from tests.test_route_auth import SECRET, mint

client = TestClient(server.app)

LIMIT_ENVIRONMENT_NAMES = [
    UPLOAD_MAX_REQUEST_MEGABYTES_ENV,
    UPLOAD_MAX_FILE_MEGABYTES_ENV,
    UPLOAD_MAX_ZIP_ENTRIES_ENV,
    UPLOAD_MAX_UNZIPPED_MEGABYTES_ENV,
]
BUDGET_ENVIRONMENT_NAMES = [
    UPLOAD_FILES_PER_DAY_ENV,
    UPLOAD_FILES_PER_CALLER_PER_DAY_ENV,
    UPLOAD_MEGABYTES_PER_DAY_ENV,
    UPLOAD_MEGABYTES_PER_CALLER_PER_DAY_ENV,
]

SMALL_LIMIT_BYTES = 4096
BOUNDARY = "upload-limits-boundary"
STREAMED_CHUNK_BYTES = 1024
STREAMED_CHUNKS_OFFERED = 1000
MANY_ZIP_ENTRIES = 50
ZIP_ENTRY_LIMIT = 10
UNZIPPED_LIMIT_BYTES = BYTES_PER_MEGABYTE
BOMB_ENTRY_BYTES = 8 * BYTES_PER_MEGABYTE


def geojson(name="parcel", padding=0):
    return (
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{"name":"%s"},'
        '"geometry":{"type":"Point","coordinates":[1.0,2.0]}}]}' % (name + "x" * padding)
    ).encode()


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def caller_directories(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    monkeypatch.setattr(server, "EXEC_DIR", str(tmp_path))

    async def no_notify(text, thread_id, authorization):
        return None

    monkeypatch.setattr(server, "notify_agent", no_notify)
    return tmp_path


@pytest.fixture
def limits(monkeypatch):
    def install(**limit_bytes):
        values = dict.fromkeys(
            ("max_request_bytes", "max_file_bytes", "max_zip_entries", "max_unzipped_bytes")
        )
        monkeypatch.setattr(server, "upload_limits", UploadLimits(**(values | limit_bytes)))

    return install


@pytest.fixture
def budget(monkeypatch):
    def install(**caps):
        values = dict.fromkeys(
            (
                "files_per_day",
                "files_per_caller_per_day",
                "bytes_per_day",
                "bytes_per_caller_per_day",
            )
        )
        monkeypatch.setattr(server, "upload_budget", UploadBudget(**(values | caps)))

    return install


def upload(subject, filename, content):
    return client.post(
        "/upload",
        files={"file": (filename, content)},
        headers={"Authorization": f"Bearer {mint(sub=subject)}"},
    )


def written_files(root):
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != utils.CATALOGUE_NAME
    )


def test_a_normal_upload_still_works(caller_directories, limits, budget):
    limits(
        max_request_bytes=SMALL_LIMIT_BYTES,
        max_file_bytes=SMALL_LIMIT_BYTES,
        max_zip_entries=ZIP_ENTRY_LIMIT,
        max_unzipped_bytes=UNZIPPED_LIMIT_BYTES,
    )
    budget(files_per_caller_per_day=1, bytes_per_caller_per_day=SMALL_LIMIT_BYTES)

    response = upload("alice", "parcels.geojson", geojson())

    assert response.status_code == 200
    assert response.json()["row_count"] == 1
    assert [path.split("/")[-1] for path in written_files(caller_directories)] == [
        "parcels.geojson"
    ]


def test_a_zipped_upload_within_the_limits_still_unzips(caller_directories, limits):
    import geopandas as gpd

    limits(max_zip_entries=ZIP_ENTRY_LIMIT, max_unzipped_bytes=UNZIPPED_LIMIT_BYTES)
    package = caller_directories / "parcels.gpkg"
    gpd.read_file(io.BytesIO(geojson())).to_file(package, driver="GPKG")
    archive = zipped([("layer/parcels.gpkg", package.read_bytes())])
    package.unlink()

    response = upload("alice", "bundle.zip", archive)

    assert response.status_code == 200
    assert response.json()["name"] == "parcels"
    assert [path.split("/", 2)[-1] for path in written_files(caller_directories)] == [
        "bundle/layer/parcels.gpkg"
    ]


def test_a_body_over_the_request_limit_gets_413(caller_directories, limits):
    limits(max_request_bytes=SMALL_LIMIT_BYTES)

    response = upload("alice", "parcels.geojson", geojson(padding=SMALL_LIMIT_BYTES))

    assert response.status_code == 413
    assert written_files(caller_directories) == []


def test_a_body_with_no_length_stops_being_read_at_the_limit(caller_directories, limits):
    limits(max_request_bytes=SMALL_LIMIT_BYTES)
    preamble = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="parcels.geojson"\r\n'
        "Content-Type: application/geo+json\r\n\r\n"
    ).encode()
    chunks_pulled = 0
    statuses = []

    async def receive():
        nonlocal chunks_pulled
        chunks_pulled += 1
        body = preamble if chunks_pulled == 1 else b"x" * STREAMED_CHUNK_BYTES
        more_body = chunks_pulled < STREAMED_CHUNKS_OFFERED
        return {"type": "http.request", "body": body, "more_body": more_body}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "root_path": "",
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
            (b"authorization", f"Bearer {mint(sub='alice')}".encode()),
        ],
    }

    asyncio.run(server.app(scope, receive, send))

    assert statuses == [413]
    assert chunks_pulled * STREAMED_CHUNK_BYTES < 2 * SMALL_LIMIT_BYTES
    assert chunks_pulled < STREAMED_CHUNKS_OFFERED
    assert written_files(caller_directories) == []


def test_a_file_over_the_file_limit_gets_413(caller_directories, limits):
    limits(max_file_bytes=SMALL_LIMIT_BYTES)

    response = upload("alice", "parcels.geojson", geojson(padding=SMALL_LIMIT_BYTES))

    assert response.status_code == 413
    assert written_files(caller_directories) == []


def test_a_zip_with_too_many_entries_is_refused_before_unzipping(caller_directories, limits):
    limits(max_zip_entries=ZIP_ENTRY_LIMIT)
    archive = zipped([(f"part_{index}.txt", b"x") for index in range(MANY_ZIP_ENTRIES)])

    response = upload("alice", "bomb.zip", archive)

    assert response.status_code == 413
    assert written_files(caller_directories) == []
    assert not any(path.name == "bomb" for path in caller_directories.rglob("*"))


def test_a_zip_declaring_a_huge_size_is_refused_before_unzipping(caller_directories, limits):
    limits(max_unzipped_bytes=UNZIPPED_LIMIT_BYTES)
    archive = zipped([("zeros.shp", bytes(BOMB_ENTRY_BYTES))])
    assert len(archive) < SMALL_LIMIT_BYTES * 4

    response = upload("alice", "bomb.zip", archive)

    assert response.status_code == 413
    assert written_files(caller_directories) == []
    assert not any(path.name == "bomb" for path in caller_directories.rglob("*"))


@pytest.mark.parametrize("entry", ["../escaped.shp", "/tmp/escaped.shp", "a/../../escaped.shp"])
def test_a_zip_entry_that_climbs_out_is_refused(caller_directories, limits, entry):
    limits()

    response = upload("alice", "climb.zip", zipped([(entry, b"x"), ("ok.shp", b"x")]))

    assert response.status_code == 400
    assert written_files(caller_directories) == []


@pytest.mark.parametrize("filename", ["..zip", "...zip", "...geojson"])
def test_a_filename_with_no_name_before_its_extension_is_refused(
    caller_directories, limits, filename
):
    limits()

    response = upload("alice", filename, zipped([("parcels.shp", b"x")]))

    assert response.status_code == 400
    assert "needs a name before its extension" in response.json()["detail"]
    assert written_files(caller_directories) == []


def test_the_callers_upload_count_refuses_the_next_upload_with_429(
    caller_directories, budget
):
    budget(files_per_caller_per_day=2)

    assert upload("alice", "first.geojson", geojson()).status_code == 200
    assert upload("alice", "second.geojson", geojson()).status_code == 200
    refused = upload("alice", "third.geojson", geojson())

    assert refused.status_code == 429
    assert refused.json()["detail"] == CALLER_FILES_SPENT_REPLY
    assert not any(path.name == "third.geojson" for path in caller_directories.rglob("*"))
    assert upload("bob", "first.geojson", geojson()).status_code == 200


def test_a_zip_is_charged_its_unzipped_size_against_the_daily_bytes(
    caller_directories, limits, budget
):
    limits()
    budget(bytes_per_caller_per_day=UNZIPPED_LIMIT_BYTES)
    archive = zipped([("zeros.shp", bytes(2 * UNZIPPED_LIMIT_BYTES))])
    assert len(archive) < UNZIPPED_LIMIT_BYTES

    refused = upload("alice", "bundle.zip", archive)

    assert refused.status_code == 429
    assert refused.json()["detail"] == CALLER_BYTES_SPENT_REPLY
    assert written_files(caller_directories) == []


@pytest.mark.parametrize("value", [None, "0", ""])
def test_unset_or_zero_means_no_limit(monkeypatch, value):
    for name in LIMIT_ENVIRONMENT_NAMES + BUDGET_ENVIRONMENT_NAMES:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    assert UploadLimits.from_environment() == UploadLimits(None, None, None, None)
    upload_budget = UploadBudget.from_environment()
    for daily_budget in (upload_budget.file_budget, upload_budget.byte_budget):
        assert daily_budget.limit_per_day is None
        assert daily_budget.limit_per_caller_per_day is None


def test_megabyte_variables_are_read_as_megabytes(monkeypatch):
    monkeypatch.setenv(UPLOAD_MAX_FILE_MEGABYTES_ENV, "3")

    assert UploadLimits.from_environment().max_file_bytes == 3 * BYTES_PER_MEGABYTE


@pytest.mark.parametrize("name", LIMIT_ENVIRONMENT_NAMES + BUDGET_ENVIRONMENT_NAMES)
@pytest.mark.parametrize("value", ["-1", "ten", "2.5"])
def test_an_invalid_limit_fails_startup(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError) as raised:
        UploadLimits.from_environment()
        UploadBudget.from_environment()

    assert name in str(raised.value)
