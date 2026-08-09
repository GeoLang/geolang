"""Markers a tool writes into its text output for the client to act on.

A tool returns one string. Anything the client has to do rather than read
travels as a ``MARKER:{json}`` line inside it, so the same result serves a
transcript, the AG-UI stream and the live document bridge.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

UI_SPEC_MARKER = "__UI_SPEC__:"
VIEWER_COMMAND_MARKER = "__VIEWER_CMD__:"


def marker_payloads(content: str, marker: str):
    """Every JSON payload on a ``MARKER:{json}`` line of a tool's output."""
    for part in content.split(marker)[1:]:
        line = part.split("\n")[0].strip()
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning(f"unparseable {marker} payload: {line[:120]}")
            continue
        yield payload
