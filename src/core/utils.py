"""Filesystem paths and JSON-backed state stores shared across the GeoLang API."""
from __future__ import annotations

import json
import os
from pathlib import Path

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
