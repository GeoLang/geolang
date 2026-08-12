"""Errors raised across the API and the tool layer.

Kept apart from `utils` because that module caches the tree directories at
import and the tests reload it to point them somewhere else. A class defined
there is a new class after each reload, while the tools that raise it and the
routes that catch it hold the one they imported first, so a refusal stops being
caught. Nothing here is reloaded, so there is only ever one of each.
"""


class PathRefused(ValueError):
    """A tool argument or a route named a file the caller is not allowed to name."""
