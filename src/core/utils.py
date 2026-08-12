"""Filesystem paths and JSON-backed state stores shared across the GeoLang API."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
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
DIRECTORY_NAME_CHARACTERS = "A-Za-z0-9_-"
UNSAFE_SUBJECT_CHARACTERS = re.compile(f"[^{DIRECTORY_NAME_CHARACTERS}]")
CALLER_DIRECTORY_NAME = re.compile(f"[{DIRECTORY_NAME_CHARACTERS}]+")
READABLE_SUBJECT_LENGTH = 64
SUBJECT_DIGEST_LENGTH = 32

# set by the executor, which is told the name because it cannot verify a subject
_caller_directory: ContextVar[str | None] = ContextVar("caller_directory", default=None)
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


def valid_caller_directory_name(name: str) -> bool:
    """Whether `name` is one directory of the shape `caller_directory_name` makes."""
    return CALLER_DIRECTORY_NAME.fullmatch(name) is not None


@contextmanager
def caller_directory_scope(name: str | None):
    """Run the block with `name` as the caller's directory. None reads the token."""
    reset = _caller_directory.set(name or None)
    try:
        yield
    finally:
        _caller_directory.reset(reset)


def current_caller_directory() -> str:
    """The name of the directory this call's files belong in.

    The subject comes from re-verifying the bearer, so a caller cannot pick the
    directory they land in. Anonymous is a directory of its own rather than the
    shared parent, and no subject can reach it: every other name ends in a
    hyphen and a digest, and this one has neither.

    A name in scope wins: the executor holds no signing secret, so it is told
    which directory the call belongs to by the side that could verify one.
    """
    told = _caller_directory.get()
    if told:
        return told

    # not a module-level import: every tool imports this module, auth pulls in jwt
    from src.core.auth import platform_claims

    claims = platform_claims(current_user_token())
    subject = str((claims or {}).get("sub") or "")
    if not subject:
        _warn_about_shared_outputs()
        return ANONYMOUS_OUTPUTS_DIRECTORY
    return caller_directory_name(subject)


def caller_outputs_dir() -> str:
    """Where the caller of this call writes and reads files, created if absent."""
    directory = os.path.join(OUTPUTS_ROOT, current_caller_directory())
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


def natural_earth_dirs() -> list[str]:
    """The reference sets on disk, whichever of them have been downloaded.

    A glob rather than a list of names, so a set someone downloads next is
    reachable without a code change. A set that arrives as a symlink is skipped:
    it would resolve outside the tree and widen the boundary.
    """
    return [
        str(path)
        for path in sorted(Path(EXEC_DIR).glob("natural_earth*"))
        if path.is_dir() and not path.is_symlink()
    ]


def allowed_roots() -> list[str]:
    """Directories a named file may resolve into, for a route or a tool argument.

    `EXEC_DIR` is deliberately not listed. It is the whole tree, which puts
    every other caller's outputs directory inside the boundary, so a name like
    `outputs/<their-directory>/layer.gpkg` used to resolve.

    user_data subdirs are deliberately not listed either. Confinement to the
    parents covers them, and a subdir that is itself a symlink must not be able
    to widen the boundary.
    """
    return [caller_outputs_dir(), str(USER_DATA_DIR), *natural_earth_dirs()]


def layer_search_dirs() -> list[str]:
    """Where a layer is looked up: the caller's outputs, user_data, natural earth.

    The routes and the tools read through this one list, so what one of them can
    reach cannot drift from what the other can.
    """
    user_data_subdirs = (
        [str(p) for p in USER_DATA_DIR.rglob("*") if p.is_dir()]
        if USER_DATA_DIR.exists()
        else []
    )
    return [
        caller_outputs_dir(),
        str(USER_DATA_DIR),
        *user_data_subdirs,
        *natural_earth_dirs(),
    ]


def name_candidates(filename: str) -> list[str]:
    """The name as given and its basename, each with the viewer's extension swap.

    A tool result names a layer `outputs/roads.gpkg`, which is no longer a path
    anyone can read: the file is in the caller's own directory under that name.
    """
    names = []
    for name in dict.fromkeys([filename, os.path.basename(filename)]):
        stem, ext = os.path.splitext(name)
        names += [name, stem if ext else name + ".gpkg"]
    return names


POPULATION_RASTER_NAMES = (
    "ghsl_pop.tif",
    "GHS_POP.tif",
    "ghs_pop_2020.tif",
    "ghsl_pop_2020.tif",
)


def population_raster_path() -> str | None:
    """The shared GHS-POP raster, or None when nobody has put one on disk.

    Reference data rather than anyone's file, and the names are fixed here
    rather than taken from a caller. That is why the exec dir root is searched
    at all: nothing a caller writes may name a file there.
    """
    for directory in (caller_outputs_dir(), str(USER_DATA_DIR), EXEC_DIR):
        for name in POPULATION_RASTER_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                return candidate
    return None


class PathRefused(ValueError):
    """A tool argument named a file the caller is not allowed to name."""


def tool_input_path_or_none(argument: str, value: str) -> str | None:
    """The file `argument` names, or None when it names no file the caller has.

    For an argument that is a filename only some of the time: `service_path` is
    either a layer of the caller's own or an OSM category name. An absolute path
    is refused rather than falling through to the other meaning, so nothing is
    reachable by naming someone else's file.
    """
    if not value:
        return None
    if os.path.isabs(value):
        raise PathRefused(
            f"{argument} must be a filename in your own outputs, in user_data, "
            f"or in a natural earth set, not an absolute path: '{value}'"
        )
    return resolve_under(name_candidates(value), layer_search_dirs(), allowed_roots())


def tool_input_path(argument: str, value: str) -> str:
    """The file `argument` names, or a refusal saying what was wanted instead.

    An absolute path is refused rather than looked up under its basename. It
    names another caller's file often enough to be the reason this exists, and
    quietly opening a same-named file of the caller's own would answer a
    different question than the one asked.
    """
    path = tool_input_path_or_none(argument, value)
    if not path:
        raise PathRefused(
            f"{argument}: no file named '{value}' in your outputs, in user_data, "
            "or in the natural earth sets. Call list_outputs to see what exists."
        )
    return path


def tool_output_path(argument: str, filename: str) -> str:
    """Where a tool writes `filename`, always inside the caller's own directory.

    A name carrying a directory part is refused rather than cut down to its
    basename: two callers asking for two different paths would otherwise land
    on one file. The resolved path is checked too, so a name that is already a
    symlink to somewhere else is written through rather than followed.
    """
    single_component = filename and filename == os.path.basename(filename)
    if not single_component or filename in (".", ".."):
        raise PathRefused(
            f"{argument} names a file inside your own outputs directory, so it "
            f"must be one filename with no directory part: '{filename}'"
        )
    directory = Path(caller_outputs_dir())
    path = directory / filename
    if not path.resolve().is_relative_to(directory.resolve()):
        raise PathRefused(
            f"{argument} names '{filename}', which already points out of your "
            "outputs directory. Pick another name."
        )
    return str(path)


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
