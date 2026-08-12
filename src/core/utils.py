"""Filesystem paths and JSON-backed state stores shared across the GeoLang API."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SIBYL_URL = os.environ.get("SIBYL_URL", "http://localhost:8090")
# Default to the geolang repo root (three levels up from src/core/utils.py),
# so the API works without TOOL_EXEC_DIR set regardless of checkout location.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXEC_DIR = os.environ.get("TOOL_EXEC_DIR", str(_REPO_ROOT))
OUTPUTS_DIR = os.path.join(EXEC_DIR, "outputs")
SHARES_FILE = os.path.join(EXEC_DIR, ".shares.json")
# layer data published to a live document, readable without a platform token by
# whoever holds the file's token
LIVE_DATA_DIR = Path(EXEC_DIR) / "live_data"
USER_DATA_DIR = Path(EXEC_DIR) / "user_data"
CATALOGUE_FILE = USER_DATA_DIR / "catalogue.json"


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
