"""Which manifests a caller has had planned, so run_workflow can refuse the rest.

plan_workflow validates a manifest and shows it to the user. run_workflow
executes it. Only the persona connected the two, so a model that skipped the
plan could still run a pipeline the user never saw. plan_workflow now records
the digest of the manifest text it validated, and run_workflow refuses a
manifest that has no record.

The digest is of the confined manifest text, the exact bytes plan_workflow
posted to /validate and put in the plan the user read. run_workflow confines
before it looks, and confinement is idempotent, so the manifest the model wrote
and the confined one the viewer's approve button sends both land on that digest,
and anything else fails closed.

A record is kept until it expires rather than consumed by the run: a retry of
the same approved manifest is the same reviewed pipeline, and making it re-plan
sends the model looking for another way to do the work.

Records live in this process only. plan_workflow and run_workflow both execute
here, or both in the executor, so one process sees both halves. A restart
between them loses the record and the run is refused, which asks for a re-plan.

This sits in core rather than beside the two tools because the tool loader
imports tool modules as the top-level `tools` package and reloads them: a store
next to them would exist once per import name, and the plan would land in one
copy while the run looked in the other.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from src.core.utils import current_caller_directory

PLAN_LIFETIME_SECONDS = 3600.0
PLANS_KEPT_PER_CALLER = 32

# caller directory -> digest -> when it was planned
_planned: dict[str, OrderedDict[str, float]] = {}


def _digest(manifest_toml: str) -> str:
    return hashlib.sha256(manifest_toml.encode("utf-8")).hexdigest()


def _forget_expired(now: float) -> None:
    # pop, not del: tool calls run in a threadpool and another one may have
    # swept the same entry
    for caller, plans in list(_planned.items()):
        for digest, planned_at in list(plans.items()):
            if now - planned_at > PLAN_LIFETIME_SECONDS:
                plans.pop(digest, None)
        if not plans:
            _planned.pop(caller, None)


def record_planned_manifest(manifest_toml: str) -> None:
    """Remember that this caller has had exactly this manifest text planned."""
    now = time.monotonic()
    _forget_expired(now)
    plans = _planned.setdefault(current_caller_directory(), OrderedDict())
    digest = _digest(manifest_toml)
    plans.pop(digest, None)
    plans[digest] = now
    while len(plans) > PLANS_KEPT_PER_CALLER:
        plans.popitem(last=False)


def manifest_was_planned(manifest_toml: str) -> bool:
    """Whether this caller planned exactly this text and the record still holds."""
    now = time.monotonic()
    _forget_expired(now)
    return _digest(manifest_toml) in _planned.get(current_caller_directory(), {})


def forget_planned_manifests() -> None:
    """Drop every record, so a test starts with nothing approved."""
    _planned.clear()
