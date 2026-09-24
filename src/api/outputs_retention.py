"""Age-based cleanup of the outputs and user_data volumes.

`DELETE /outputs/{filename}` only reaches the caller's own directory, and a file
no tool announced is deleted by nothing, so without a sweep the volume only
grows. Uploads have no delete route at all.

The sweep runs in the API server process. The executor mounts the same volumes
and deliberately does not run it, so one process is the only deleter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from src.core import utils

logger = logging.getLogger(__name__)

RETENTION_DAYS_ENV = "GEOLANG_OUTPUTS_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30
USER_DATA_RETENTION_DAYS_ENV = "GEOLANG_USER_DATA_RETENTION_DAYS"
USER_DATA_DEFAULT_RETENTION_DAYS = 0
SECONDS_PER_DAY = 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = SECONDS_PER_DAY


def _configured_days(name: str, default: int) -> int:
    configured = os.environ.get(name, "").strip()
    return int(configured) if configured else default


def retention_days() -> int:
    """How long an output file is kept. Zero or less keeps everything forever."""
    return _configured_days(RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS)


def user_data_retention_days() -> int:
    return _configured_days(USER_DATA_RETENTION_DAYS_ENV, USER_DATA_DEFAULT_RETENTION_DAYS)


def sweep_outputs() -> tuple[int, int]:
    return sweep(Path(utils.OUTPUTS_ROOT), retention_days())


def sweep_user_data() -> tuple[int, int]:
    return sweep(Path(utils.USER_DATA_ROOT), user_data_retention_days())


def sweep(root: Path, days: int) -> tuple[int, int]:
    """Delete files under `root` past the retention window, and the directories they emptied.

    Only regular files inside a caller's own directory are deleted, at any depth.
    A symlink is neither followed nor removed, and a caller directory that is
    itself a symlink is skipped, so nothing outside the root is touched.

    Returns the files removed and the bytes freed.
    """
    if days <= 0:
        return 0, 0

    root = root.resolve()
    if not root.is_dir():
        return 0, 0
    cutoff = time.time() - days * SECONDS_PER_DAY

    removed = 0
    freed = 0
    for directory in _caller_directories(root):
        # bottom up, so a folder is looked at after everything inside it
        for folder, _, _ in os.walk(directory, topdown=False):
            for entry in _entries(folder):
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not Path(entry.path).resolve().is_relative_to(root):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_mtime >= cutoff:
                        continue
                    os.remove(entry.path)
                except OSError:
                    logger.exception(f"retention could not delete {entry.path}")
                    continue
                removed += 1
                freed += stat.st_size
            _remove_if_empty(folder)

    logger.info(
        f"{root.name} retention: removed {removed} files, freed {freed} bytes, "
        f"older than {days} days"
    )
    return removed, freed


def _caller_directories(root: Path) -> list[str]:
    """The per-caller directories under the root, not symlinks, not the shares."""
    return [
        entry.path
        for entry in _entries(root)
        if entry.is_dir(follow_symlinks=False)
        and utils.valid_caller_directory_name(entry.name)
    ]


def _entries(directory) -> list[os.DirEntry]:
    """What is in `directory` now, or nothing when it went away mid-sweep."""
    try:
        with os.scandir(directory) as scan:
            return list(scan)
    except OSError:
        logger.exception(f"retention could not read {directory}")
        return []


def _remove_if_empty(directory: str) -> None:
    """Remove a directory the sweep emptied. One file left in it keeps it."""
    if _entries(directory):
        return
    try:
        os.rmdir(directory)
    except OSError:
        logger.exception(f"retention could not remove {directory}")


async def sweep_periodically() -> None:
    """One pass now, then one a day, in a thread so the walk does not block the loop."""
    while True:
        await asyncio.to_thread(sweep_outputs)
        await asyncio.to_thread(sweep_user_data)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
