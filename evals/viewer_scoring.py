"""Deterministic scoring of the viewer_control calls a model made for a prompt.

The task names one action and the arguments a correct answer carries. Every one
of those is a check worth a point, and the call that matches best is the one
scored: a model that tried something else first and then got it right has
answered the prompt.

An action the viewer's catalogue lists is called through `viewer_control`'s
`run`, so the check on the action also checks the catalogue name. The fixed
actions carry their arguments at the top level of the tool call instead.
"""

import json
import tomllib
from pathlib import Path

from evals.scoring import Check, Result, values_match

# arguments that name something in the viewer state, where the id and the name
# both reach the same thing
IDENTIFIER_ARGUMENTS = frozenset(
    {"layer", "project", "document", "feed", "dataset"}
)
RUN_ACTION = "run"
VIEWER_CONTROL_TOOL = "viewer_control"


class ViewerTask:
    """One golden task: a prompt plus the viewer call a correct answer makes."""

    def __init__(self, data: dict, path: Path | None = None):
        self.path = path
        self.id = data["id"]
        self.prompt = data["prompt"]
        self.notes = data.get("notes", "")
        expect = data.get("expect") or {}
        self.action = expect.get("action")
        self.name = expect.get("name")
        self.args = dict(expect.get("args") or {})
        self.tolerance = dict(data.get("tolerance") or {})
        self.snapshot = dict(data.get("snapshot") or {})
        if not self.action:
            raise ValueError(f"task {self.id} expects no action")
        if self.action == RUN_ACTION and not self.name:
            raise ValueError(f"task {self.id} runs an action with no name")

    def snapshot_for(self, shared: dict) -> dict:
        """The viewer state this task is asked from."""
        return {**shared, **self.snapshot}

    @property
    def target(self) -> str:
        """What the model has to call, as one string for a check name."""
        return f"{self.action} {self.name}" if self.name else self.action

    def __repr__(self):
        return f"ViewerTask({self.id})"


def load_task(path) -> ViewerTask:
    path = Path(path)
    with open(path, "rb") as fh:
        return ViewerTask(tomllib.load(fh), path)


def load_tasks(directory) -> list:
    """Every task in the directory, ordered by id so reports are comparable."""
    tasks = [load_task(p) for p in sorted(Path(directory).glob("*.toml"))]
    ids = [t.id for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return tasks


def equivalent_identifiers(snapshot) -> list:
    """Groups of strings that name the same thing in the viewer state.

    Anything the viewer describes with both an id and a name can be asked for by
    either, so a model that answered with the id is right.
    """
    groups = []

    def walk(value):
        if isinstance(value, dict):
            # the live document is the one thing the state keys by documentId
            identifier = value.get("id") or value.get("documentId")
            name = value.get("name")
            if isinstance(identifier, str) and isinstance(name, str):
                groups.append({identifier.strip().lower(), name.strip().lower()})
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(snapshot)
    return groups


def _same_thing(expected, actual, groups) -> bool:
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    wanted, got = expected.strip().lower(), actual.strip().lower()
    return any(wanted in group and got in group for group in groups)


def call_action(call: dict) -> str:
    return str(call.get("action") or "")


# the fields of a run call that are not the run's own parameters
RUN_CALL_FIELDS = {"action", "name", "args", "url"}


def read_arguments(value):
    """The arguments as an object, or None when they cannot be read as one.

    Mirrors the viewer's `readArguments`: nothing at all is an empty object, and
    so is empty text, but text that will not decode is a call the viewer refuses.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        if value.strip() == "":
            return {}
        try:
            return read_arguments(json.loads(value))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _object_text(value) -> dict:
    """`value` decoded as an object, or {} when it is not one."""
    return read_arguments(value) or {}


def call_arguments(call: dict) -> dict:
    """The arguments the call carries, wherever this action puts them.

    A run's parameters come as plain fields of the call, or as JSON text in
    `args`, or in `url`, which is where one model writes the object. The tool
    accepts all three, so the scorer reads all three.
    """
    if call_action(call) != RUN_ACTION:
        return {k: v for k, v in call.items() if k != "action"}
    written = _object_text(call.get("args")) or _object_text(call.get("url"))
    flat = {k: v for k, v in call.items() if k not in RUN_CALL_FIELDS}
    return {**written, **flat}


def call_target(call: dict) -> str:
    action = call_action(call)
    if action != RUN_ACTION:
        return action
    return f"{action} {call.get('name')}"


def _unwrap_singleton(want, got):
    """A lone scalar inside an array, read as the viewer reads it.

    Mirrors the viewer's `unwrapSingleton`: only where a scalar is expected,
    so an expected array keeps its one element.
    """
    if isinstance(want, (list, dict)):
        return got
    if isinstance(got, list) and len(got) == 1:
        return got[0]
    return got


def score_call(task: ViewerTask, call: dict, groups) -> list:
    """The checks one viewer_control call earns against the task."""
    hit = call_target(call) == task.target
    checks = [
        Check(
            f"calls {task.target}",
            hit,
            "" if hit else f"called {call_target(call)}",
        )
    ]
    arguments = call_arguments(call)
    for key, want in task.args.items():
        got = _unwrap_singleton(want, arguments.get(key))
        ok = got is not None and (
            values_match(want, got, task.tolerance.get(key))
            or (key in IDENTIFIER_ARGUMENTS and _same_thing(want, got, groups))
        )
        checks.append(Check(f"{key} = {want}", ok, "" if ok else f"got {got!r}"))
    return checks


def _nothing_called(task: ViewerTask) -> list:
    checks = [Check(f"calls {task.target}", False, "no viewer_control call")]
    checks += [Check(f"{key} = {want}", False, "") for key, want in task.args.items()]
    return checks


def score_calls(task: ViewerTask, calls: list, snapshot=None) -> Result:
    """Score the viewer_control call that answers the prompt best."""
    usable = [c for c in calls if isinstance(c, dict)]
    if not usable:
        return Result(task.id, _nothing_called(task), "")

    groups = equivalent_identifiers(snapshot or {})
    scored = [(score_call(task, call, groups), call) for call in usable]
    checks, best = max(scored, key=lambda pair: sum(c.passed for c in pair[0]))
    return Result(task.id, checks, json.dumps(best, sort_keys=True))
