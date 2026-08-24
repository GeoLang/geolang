"""The outputs volume is swept by age, because nothing else deletes an old file.

`DELETE /outputs/{filename}` reaches the caller's own directory and no other, so
it can never clean up after anyone else, and a file no tool announced is deleted
by nothing at all.

The sweep reads `OUTPUTS_ROOT` at call time, so these tests point the copy in
`utils` at a tmp_path.
"""

import os
import time
from pathlib import Path

import pytest

from src.api.outputs_retention import (
    DEFAULT_RETENTION_DAYS,
    RETENTION_DAYS_ENV,
    SECONDS_PER_DAY,
    retention_days,
    sweep_outputs,
)
from src.core import utils
from src.core.utils import caller_directory_name

ALICE = caller_directory_name("alice")
BOB = caller_directory_name("bob")
FILE_BYTES = 10


@pytest.fixture
def outputs(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    monkeypatch.setattr(utils, "OUTPUTS_ROOT", str(root))
    monkeypatch.setenv(RETENTION_DAYS_ENV, "30")
    return root


def aged(path: Path, days: float) -> Path:
    """A file last written `days` ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * FILE_BYTES)
    written = time.time() - days * SECONDS_PER_DAY
    os.utime(path, (written, written))
    return path


def test_the_window_defaults_to_thirty_days(monkeypatch):
    monkeypatch.delenv(RETENTION_DAYS_ENV, raising=False)

    assert retention_days() == DEFAULT_RETENTION_DAYS == 30


def test_retention_deletes_an_over_age_file_in_any_callers_directory(outputs):
    stale = aged(outputs / ALICE / "old.gpkg", 45)
    fresh = aged(outputs / BOB / "new.gpkg", 1)

    assert sweep_outputs() == (1, FILE_BYTES)
    assert not stale.exists()
    assert fresh.exists()


def test_an_emptied_caller_directory_is_removed(outputs):
    aged(outputs / ALICE / "old.gpkg", 45)
    aged(outputs / BOB / "new.gpkg", 1)

    sweep_outputs()

    assert not (outputs / ALICE).exists()
    assert (outputs / BOB).is_dir()


def test_a_symlink_out_of_the_tree_is_not_followed(outputs, tmp_path):
    secret = aged(tmp_path / "outside" / "secret.gpkg", 900)
    escape = outputs / ALICE / "escape.gpkg"
    escape.parent.mkdir(parents=True)
    escape.symlink_to(secret)

    assert sweep_outputs() == (0, 0)
    assert secret.exists()
    assert escape.is_symlink()
    # the symlink is not a file the sweep removed, so the directory stays too
    assert (outputs / ALICE).is_dir()


def test_a_symlinked_caller_directory_is_skipped(outputs, tmp_path):
    stale = aged(tmp_path / "outside" / "secret.gpkg", 900)
    (outputs / ALICE).symlink_to(tmp_path / "outside")

    assert sweep_outputs() == (0, 0)
    assert stale.exists()


def test_retention_of_zero_deletes_nothing(outputs, monkeypatch):
    monkeypatch.setenv(RETENTION_DAYS_ENV, "0")
    stale = aged(outputs / ALICE / "old.gpkg", 900)

    assert sweep_outputs() == (0, 0)
    assert stale.exists()
    assert (outputs / ALICE).is_dir()
