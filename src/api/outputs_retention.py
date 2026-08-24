"""Age-based cleanup of the outputs volume.

`DELETE /outputs/{filename}` only reaches the caller's own directory, and a file
no tool announced is deleted by nothing, so without a sweep the volume only
grows.

The sweep runs in the API server process. The executor mounts the same volume
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
SECONDS_PER_DAY = 24 * 60 * 60
SWEEP_INTERVAL_SECONDS = SECONDS_PER_DAY


def retention_days() -> int:
    """How long an output file is kept. Zero or less keeps everything forever."""
    configured = os.environ.get(RETENTION_DAYS_ENV, "").strip()
    return int(configured) if configured else DEFAULT_RETENTION_DAYS


def sweep_outputs() -> tuple[int, int]:
    """Delete output files past the retention window, and the directories they emptied.

    Only regular files one level inside a caller's own directory are deleted. A
    symlink is neither followed nor removed, and a caller directory that is
    itself a symlink is skipped, so nothing outside the outputs root is touched.

    Returns the files removed and the bytes freed.
    """
    days = retention_days()
    if days <= 0:
        return 0, 0

    root = Path(utils.OUTPUTS_ROOT).resolve()
    if not root.is_dir():
        return 0, 0
    cutoff = time.time() - days * SECONDS_PER_DAY

    removed = 0
    freed = 0
    for directory in _caller_directories(root):
        for entry in _entries(directory):
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
                logger.exception(f"outputs retention could not delete {entry.path}")
                continue
            removed += 1
            freed += stat.st_size
        _remove_if_empty(directory)

    logger.info(
        f"outputs retention: removed {removed} files, freed {freed} bytes, "
        f"older than {days} days"
    )
    return removed, freed


def _caller_directories(root: Path) -> list[str]:
    """The per-caller directories under the outputs root, symlinked ones skipped."""
    return [
        entry.path for entry in _entries(root) if entry.is_dir(follow_symlinks=False)
    ]


def _entries(directory) -> list[os.DirEntry]:
    """What is in `directory` now, or nothing when it went away mid-sweep."""
    try:
        with os.scandir(directory) as scan:
            return list(scan)
    except OSError:
        logger.exception(f"outputs retention could not read {directory}")
        return []


def _remove_if_empty(directory: str) -> None:
    """Remove a caller directory the sweep emptied. One file left in it keeps it."""
    if _entries(directory):
        return
    try:
        os.rmdir(directory)
    except OSError:
        logger.exception(f"outputs retention could not remove {directory}")


async def sweep_outputs_periodically() -> None:
    """One pass now, then one a day, in a thread so the walk does not block the loop."""
    if retention_days() <= 0:
        logger.info(f"outputs retention off, {RETENTION_DAYS_ENV} is 0")
        return
    while True:
        await asyncio.to_thread(sweep_outputs)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
