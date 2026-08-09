"""The suite runs as the authless dev stack does, and says so.

Importing the app now refuses a deployment with no secret and no opt-out, so the
opt-out has to be set before any test imports it. `setdefault` so a run that
already chose a mode keeps it.
"""

import os

from src.core.auth import UNAUTHENTICATED_ENV

os.environ.setdefault(UNAUTHENTICATED_ENV, "1")
