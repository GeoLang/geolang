"""The JSON files the API keeps state in survive a write that does not finish.

Each store is read whole, changed, and written back whole. Opening the target
truncates it first, so a write that dies partway used to leave a file that no
longer parses, and for shares that was silently read as "no shares" and then
written back over the real ones.
"""

import json
from pathlib import Path

import pytest

from src.core import utils
from src.core.utils import caller_directory_scope

BOB = "bob-fedcba9876543210"


SHARE_ID = "kXv3-2_QeR9tYuI0pAsDfg"
OLD_SHARE_ID = "3f2a9c1e"


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(root))
    return root


def test_shares_are_kept_on_the_outputs_volume():
    """The outputs volume is the part of the tree that survives a rebuild."""
    assert utils.shares_directory().parent == Path(utils.OUTPUTS_ROOT)


def test_no_caller_directory_can_be_named_like_the_shares_directory():
    assert not utils.valid_caller_directory_name(utils.shares_directory().name)


def test_the_first_share_creates_a_missing_outputs_directory(outputs):
    utils.save_share(SHARE_ID, {"title": "a map"})

    assert utils.load_share(SHARE_ID) == {"title": "a map"}


def test_each_share_is_a_file_of_its_own(outputs):
    utils.save_share(SHARE_ID, {"title": "a map"})
    utils.save_share(OLD_SHARE_ID, {"title": "another"})

    assert sorted(path.name for path in utils.shares_directory().iterdir()) == [
        f"{OLD_SHARE_ID}.json",
        f"{SHARE_ID}.json",
    ]
    assert json.loads(utils.share_file(SHARE_ID).read_text()) == {"title": "a map"}


def test_a_write_that_fails_leaves_the_share_that_was_there(outputs):
    utils.save_share(SHARE_ID, {"title": "a map"})

    with pytest.raises(TypeError):
        utils.save_share(SHARE_ID, {"title": object()})

    assert utils.load_share(SHARE_ID) == {"title": "a map"}


def test_a_share_that_does_not_parse_is_raised_rather_than_read_as_missing(outputs):
    utils.share_file(SHARE_ID).parent.mkdir(parents=True)
    utils.share_file(SHARE_ID).write_text('{"tit')

    with pytest.raises(json.JSONDecodeError):
        utils.load_share(SHARE_ID)


@pytest.mark.parametrize("share_id", ["../secret", "..", "", "a/b", "a" * 65])
def test_an_id_that_is_not_a_share_id_reads_nothing(outputs, share_id):
    (outputs / "secret.json").parent.mkdir(parents=True)
    (outputs / "secret.json").write_text('{"title": "not a share"}')

    assert utils.load_share(share_id) is None
    with pytest.raises(utils.PathRefused):
        utils.save_share(share_id, {"title": "a map"})


def test_the_file_of_all_shares_is_split_into_one_file_each(outputs):
    outputs.mkdir()
    all_shares = outputs / utils.ALL_SHARES_FILE_NAME
    all_shares.write_text(
        json.dumps(
            {
                SHARE_ID: {"title": "a map"},
                OLD_SHARE_ID: {"title": "an old map"},
                "../escape": {"title": "not a share id"},
            }
        )
    )

    utils.split_all_shares_file()
    utils.split_all_shares_file()

    assert utils.load_share(SHARE_ID) == {"title": "a map"}
    assert utils.load_share(OLD_SHARE_ID) == {"title": "an old map"}
    assert not all_shares.exists()
    assert not (outputs / "escape.json").exists()
    assert len(list(utils.shares_directory().iterdir())) == 2


def test_no_file_of_all_shares_is_nothing_to_split(outputs):
    utils.split_all_shares_file()

    assert not outputs.exists()


@pytest.fixture
def user_data(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "EXEC_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")
    with caller_directory_scope(BOB):
        yield Path(utils.caller_user_data_dir())


def uploaded(user_data, name):
    (user_data / name).write_text("{}")
    return {"name": name, "relative_path": f"user_data/{BOB}/{name}"}


def test_a_write_that_fails_leaves_the_uploads_the_catalogue_listed(user_data):
    roads = uploaded(user_data, "roads.geojson")
    utils.save_catalogue([roads])

    with pytest.raises(TypeError):
        utils.save_catalogue([{"name": object()}])

    assert utils.load_catalogue() == [roads]


def test_an_upload_whose_file_is_gone_is_not_listed(user_data):
    roads = uploaded(user_data, "roads.geojson")
    parcels = uploaded(user_data, "parcels.geojson")
    utils.save_catalogue([roads, parcels])

    (user_data / "parcels.geojson").unlink()

    assert utils.load_catalogue() == [roads]
