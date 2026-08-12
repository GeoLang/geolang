"""Filesystem paths and JSON-backed state stores shared across the GeoLang API."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from src.core.user_token import current_user_token

logger = logging.getLogger(__name__)

SIBYL_URL = os.environ.get("SIBYL_URL", "http://localhost:8090")
# Default to the geolang repo root (three levels up from src/core/utils.py),
# so the API works without TOOL_EXEC_DIR set regardless of checkout location.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXEC_DIR = os.environ.get("TOOL_EXEC_DIR", str(_REPO_ROOT))
# one directory per caller, never written to directly
OUTPUTS_ROOT = os.path.join(EXEC_DIR, "outputs")
SHARES_FILE = os.path.join(EXEC_DIR, ".shares.json")
# layer data published to a live document, readable without a platform token by
# whoever holds the file's token
LIVE_DATA_DIR = Path(EXEC_DIR) / "live_data"
USER_DATA_DIR = Path(EXEC_DIR) / "user_data"
CATALOGUE_FILE = USER_DATA_DIR / "catalogue.json"


ANONYMOUS_OUTPUTS_DIRECTORY = "anonymous"
UNSAFE_SUBJECT_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]")
READABLE_SUBJECT_LENGTH = 64
SUBJECT_DIGEST_LENGTH = 32

_warned_about_shared_outputs = False


def caller_directory_name(subject: str) -> str:
    """The directory name a token subject owns.

    The readable half is sanitized down to a charset that cannot name a parent
    or cross a directory, so it decides nothing about where the path lands. The
    digest is what makes two subjects two directories: sanitizing is lossy and
    maps `a/b` and `a:b` onto the same string, the digest of the raw subject
    does not.
    """
    readable = UNSAFE_SUBJECT_CHARACTERS.sub("_", subject)[:READABLE_SUBJECT_LENGTH]
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:SUBJECT_DIGEST_LENGTH]
    return f"{readable}-{digest}"


def caller_outputs_dir() -> str:
    """Where the caller of this call writes and reads files, created if absent.

    The subject comes from re-verifying the bearer, so a caller cannot pick the
    directory they land in. Anonymous is a directory of its own rather than the
    shared parent, and no subject can reach it: every other name ends in a
    hyphen and a digest, and this one has neither.
    """
    # not a module-level import: every tool imports this module, auth pulls in jwt
    from src.core.auth import platform_claims

    claims = platform_claims(current_user_token())
    subject = str((claims or {}).get("sub") or "")
    if subject:
        directory = os.path.join(OUTPUTS_ROOT, caller_directory_name(subject))
    else:
        _warn_about_shared_outputs()
        directory = os.path.join(OUTPUTS_ROOT, ANONYMOUS_OUTPUTS_DIRECTORY)
    os.makedirs(directory, exist_ok=True)
    return directory


def _warn_about_shared_outputs() -> None:
    """Say once that this process cannot tell its callers apart."""
    from src.core.auth import SECRET_ENV, authentication_disabled

    global _warned_about_shared_outputs
    if _warned_about_shared_outputs or authentication_disabled():
        return
    _warned_about_shared_outputs = True
    logger.warning(
        f"no verified subject on this call, so its files go to the shared "
        f"'{ANONYMOUS_OUTPUTS_DIRECTORY}' directory. Without {SECRET_ENV} this "
        "process cannot tell one caller from another."
    )


def preload_geo_stack() -> None:
    """Pay the geo-stack import cost at boot instead of on the first tool call."""
    try:
        import geopandas
        import rasterio

        logger.info(
            f"Geo stack preloaded: geopandas {geopandas.__version__}, "
            f"rasterio {rasterio.__version__}"
        )
    except Exception as e:
        logger.warning(f"Geo stack preload failed (first tool call will be slow): {e}")


def resolve_under(names, search_dirs, roots) -> str | None:
    """First of `names` found in `search_dirs`, confined to `roots`.

    Both sides are resolved before the comparison, so a `..` segment, an
    absolute name, and a symlink pointing out of the tree all miss. Directory
    order beats name order, which is the lookup the viewer already links
    against.
    """
    resolved_roots = [Path(root).resolve() for root in roots]
    for directory in search_dirs:
        for name in names:
            if not name:
                continue
            # an absolute name swallows the directory, and is then out of tree
            candidate = (Path(directory) / name).resolve()
            if not any(candidate.is_relative_to(r) for r in resolved_roots):
                continue
            if candidate.exists():
                return str(candidate)
    return None


def load_catalogue() -> list:
    if not CATALOGUE_FILE.exists():
        return []
    with open(CATALOGUE_FILE) as f:
        return json.load(f)


def save_catalogue(catalogue: list) -> None:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CATALOGUE_FILE, "w") as f:
        json.dump(catalogue, f, indent=2)


def load_shares() -> dict:
    if os.path.exists(SHARES_FILE):
        try:
            with open(SHARES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_shares(shares: dict) -> None:
    with open(SHARES_FILE, "w") as f:
        json.dump(shares, f, indent=2)
