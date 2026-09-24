from __future__ import annotations

import fcntl
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

# nominatim bans an address that sends more than one request a second
SECONDS_BETWEEN_REQUESTS = 1.1
# every tool run is its own process, so the turn is kept in a file they all lock
PACING_DIRECTORY = Path(tempfile.gettempdir()) / "geolang-external-pacing"
UNSAFE_HOST_CHARACTERS = re.compile(r"[^A-Za-z0-9.-]")


def wait_for_turn(url: str) -> None:
    host = UNSAFE_HOST_CHARACTERS.sub("_", urlsplit(url).hostname or "")
    PACING_DIRECTORY.mkdir(exist_ok=True)
    turn_file = PACING_DIRECTORY / host
    turn_file.touch()
    with open(turn_file, "r+") as last_request:
        # held until the file closes, so waiting callers line up behind this one
        fcntl.flock(last_request, fcntl.LOCK_EX)
        recorded = last_request.read().strip()
        last_request_at = float(recorded) if recorded else 0.0
        wait = min(
            SECONDS_BETWEEN_REQUESTS,
            last_request_at + SECONDS_BETWEEN_REQUESTS - time.time(),
        )
        if wait > 0:
            time.sleep(wait)
        last_request.seek(0)
        last_request.truncate()
        last_request.write(str(time.time()))


class PacedRequests(requests.auth.AuthBase):
    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        wait_for_turn(request.url)
        return request


# osmnx sends every nominatim and overpass request with these keyword arguments
def pace_osmnx_requests() -> None:
    import osmnx

    osmnx.settings.requests_kwargs = {
        **osmnx.settings.requests_kwargs,
        "auth": PacedRequests(),
    }
