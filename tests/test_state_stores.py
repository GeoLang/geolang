"""The JSON files the API keeps state in survive a write that does not finish.

Each store is read whole, changed, and written back whole. Opening the target
truncates it first, so a write that dies partway used to leave a file that no
longer parses, and for shares that was silently read as "no shares" and then
written back over the real ones.
"""

import json

import pytest

from src.core import utils
from src.core.utils import caller_directory_scope

BOB = "bob-fedcba9876543210"


@pytest.fixture
def shares_file(tmp_path, monkeypatch):
    path = tmp_path / ".shares.json"
    monkeypatch.setattr(utils, "SHARES_FILE", str(path))
    return path


def test_a_write_that_fails_leaves_the_shares_that_were_there(shares_file):
    utils.save_shares({"first": {"title": "a map"}})

    with pytest.raises(TypeError):
        utils.save_shares({"second": {"title": object()}})

    assert utils.load_shares() == {"first": {"title": "a map"}}


def test_a_finished_write_leaves_only_the_file_it_was_writing(shares_file):
    """The new copy is renamed over the old one rather than copied into it."""
    utils.save_shares({"first": {"title": "a map"}})

    assert json.loads(shares_file.read_text()) == {"first": {"title": "a map"}}
    assert [path.name for path in shares_file.parent.iterdir()] == [".shares.json"]


def test_shares_that_do_not_parse_are_raised_rather_than_read_as_empty(shares_file):
    """Read as empty, the next save would write that back over every share."""
    shares_file.write_text('{"first": {"tit')

    with pytest.raises(json.JSONDecodeError):
        utils.load_shares()


def test_a_write_that_fails_leaves_the_uploads_the_catalogue_listed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(utils, "USER_DATA_ROOT", tmp_path / "user_data")

    with caller_directory_scope(BOB):
        utils.save_catalogue([{"name": "roads"}])

        with pytest.raises(TypeError):
            utils.save_catalogue([{"name": object()}])

        assert utils.load_catalogue() == [{"name": "roads"}]
