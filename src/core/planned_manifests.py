"""Which manifests a caller has had planned and approved, so run_workflow can
refuse the rest.

plan_workflow validates a manifest and shows it to the user. run_workflow
executes it. Only the persona connected the two, so a model that skipped the
plan could still run a pipeline the user never saw. plan_workflow now records
the digest of the manifest text it validated, and run_workflow refuses a
manifest that has no record.

Whether the person at the viewer agreed was persona text too. The viewer's
approve button posts the manifest to `POST /workflow/approve` before it posts
the run, that marks the plan record approved, and run_workflow refuses a
manifest that was planned but never approved. A model can reach plan_workflow
and run_workflow; the approval route is not a tool, so it cannot reach that.

An approval only ever attaches to a plan record: approving text nobody planned
is refused rather than kept, so the two halves cannot be recorded out of order.
Planning the same text again starts a new record, and the earlier click does not
carry over to it.

The digest is of the confined manifest text, the exact bytes plan_workflow
posted to /validate and put in the plan the user read. run_workflow and the
approval route both confine before they look, and confinement is idempotent, so
the manifest the model wrote and the confined one the viewer's approve button
sends both land on that digest, and anything else fails closed.

A record is kept until it expires rather than consumed by the run: a retry of
the same approved manifest is the same reviewed pipeline, and making it re-plan
sends the model looking for another way to do the work.

Records live in this process only. plan_workflow, the approval and run_workflow
all execute here, or all in the executor, so one process sees every half. A
restart between them loses the record and the run is refused, which asks for a
re-plan.

This sits in core rather than beside the two tools because the tool loader
imports tool modules as the top-level `tools` package and reloads them: a store
next to them would exist once per import name, and the plan would land in one
copy while the run looked in the other.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from src.core.utils import current_caller_directory

PLAN_LIFETIME_SECONDS = 3600.0
PLANS_KEPT_PER_CALLER = 32


@dataclass
class PlannedManifest:
    planned_at: float
    approved: bool = False


# caller directory -> digest -> what happened to that manifest
_planned: dict[str, OrderedDict[str, PlannedManifest]] = {}


def _digest(manifest_toml: str) -> str:
    return hashlib.sha256(manifest_toml.encode("utf-8")).hexdigest()


def _forget_expired(now: float) -> None:
    # pop, not del: tool calls run in a threadpool and another one may have
    # swept the same entry
    for caller, plans in list(_planned.items()):
        for digest, record in list(plans.items()):
            if now - record.planned_at > PLAN_LIFETIME_SECONDS:
                plans.pop(digest, None)
        if not plans:
            _planned.pop(caller, None)


def _record(manifest_toml: str) -> PlannedManifest | None:
    """This caller's live record for exactly this text, if there is one."""
    now = time.monotonic()
    _forget_expired(now)
    return _planned.get(current_caller_directory(), {}).get(_digest(manifest_toml))


def record_planned_manifest(manifest_toml: str) -> None:
    """Remember that this caller has had exactly this manifest text planned."""
    now = time.monotonic()
    _forget_expired(now)
    plans = _planned.setdefault(current_caller_directory(), OrderedDict())
    digest = _digest(manifest_toml)
    plans.pop(digest, None)
    plans[digest] = PlannedManifest(planned_at=now)
    while len(plans) > PLANS_KEPT_PER_CALLER:
        plans.popitem(last=False)


def record_user_approval(manifest_toml: str) -> bool:
    """Mark this caller's plan of exactly this text as approved by the user.

    False when there is no live plan to attach the approval to, which is the
    caller being told to plan it rather than an approval kept for later.
    """
    record = _record(manifest_toml)
    if record is None:
        return False
    record.approved = True
    return True


def manifest_was_planned(manifest_toml: str) -> bool:
    """Whether this caller planned exactly this text and the record still holds."""
    return _record(manifest_toml) is not None


def manifest_was_approved(manifest_toml: str) -> bool:
    """Whether the user approved this caller's plan of exactly this text."""
    record = _record(manifest_toml)
    return record is not None and record.approved


def forget_planned_manifests() -> None:
    """Drop every record, so a test starts with nothing planned or approved."""
    _planned.clear()
