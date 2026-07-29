"""Deterministic scoring of a model-composed geodukt manifest against a golden task.

No prose grading: the inputs are the TOML the model produced and the task's
expected pipeline structure, so the same manifest always scores the same.

Every expected element of a task is one check worth one point, and the task
score is passed/total. A task that pins three parameters therefore weights
parameters more heavily, which is what pinning them is meant to express.
"""

import tomllib
from pathlib import Path

# geodukt accepts both spellings of these, so scoring must not care which
FORMAT_ALIASES = {"gpkg": "geopackage", "shp": "shapefile"}

# absolute, per-parameter overridable in a task's `tolerance` table
DEFAULT_TOLERANCE = 1e-9


class Task:
    """One golden task: a request plus the pipeline a correct answer builds."""

    def __init__(self, data: dict, path: Path | None = None):
        self.path = path
        self.id = data["id"]
        self.prompt = data["prompt"]
        self.notes = data.get("notes", "")
        # layers the prompt assumes exist, created before a stack run
        self.inputs = list(data.get("inputs") or [])
        # negative task: the request needs an operation geodukt cannot run in a
        # manifest, and the correct answer is not building one
        self.unavailable = data.get("unavailable")
        expect = data.get("expect") or {}
        self.sources = [s["format"] for s in expect.get("source") or []]
        self.sinks = [s["format"] for s in expect.get("sink") or []]
        self.transforms = list(expect.get("transform") or [])
        # parallel branches off one source have no required order between them
        self.ordered = bool(expect.get("ordered", True))
        if not self.unavailable and not (self.sources or self.transforms or self.sinks):
            raise ValueError(
                f"task {self.id} expects nothing and is not a negative task"
            )

    def __repr__(self):
        return f"Task({self.id})"


def load_task(path) -> Task:
    path = Path(path)
    with open(path, "rb") as fh:
        return Task(tomllib.load(fh), path)


def load_tasks(directory) -> list:
    """Every task in the directory, ordered by id so reports are comparable."""
    tasks = [load_task(p) for p in sorted(Path(directory).glob("*.toml"))]
    ids = [t.id for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return tasks


class Check:
    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


class Result:
    def __init__(self, task_id: str, checks: list, manifest: str = ""):
        self.task_id = task_id
        self.checks = checks
        self.manifest = manifest

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def score(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    @property
    def failures(self) -> list:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {
            "id": self.task_id,
            "score": self.score,
            "passed": self.passed,
            "total": self.total,
            "checks": [c.as_dict() for c in self.checks],
            "manifest": self.manifest,
        }


def canon_format(fmt) -> str:
    text = str(fmt or "").strip().lower()
    return FORMAT_ALIASES.get(text, text)


def values_match(expected, actual, tolerance=None) -> bool:
    if type(expected) is bool or type(actual) is bool:
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        tol = DEFAULT_TOLERANCE if tolerance is None else float(tolerance)
        return abs(float(actual) - float(expected)) <= tol
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def topological_transforms(manifest: dict) -> list:
    """Transforms in dependency order, following each step's `input` reference.

    Declaration order is not execution order: a manifest may name a transform
    before the step it consumes. Ties keep declaration order so this is stable.
    """
    transforms = [t for t in (manifest.get("transform") or []) if isinstance(t, dict)]
    by_name = {t.get("name"): t for t in transforms}
    cache = {}

    def depth(name, seen=frozenset()):
        if name in cache:
            return cache[name]
        step = by_name.get(name)
        # a source, a dangling reference, or a cycle: treat as the graph root
        if step is None or name in seen:
            return 0
        value = 1 + depth(step.get("input"), seen | {name})
        cache[name] = value
        return value

    order = {id(t): i for i, t in enumerate(transforms)}
    return sorted(transforms, key=lambda t: (depth(t.get("name")), order[id(t)]))


def _match_transforms(expected: list, actual: list) -> list:
    """Pair each expected transform with the earliest unused actual of that operation."""
    used = set()
    pairs = []
    for exp in expected:
        want = str(exp.get("operation", "")).strip().lower()
        hit = None
        for i, act in enumerate(actual):
            if i in used:
                continue
            if str(act.get("operation", "")).strip().lower() == want:
                hit = i
                break
        if hit is not None:
            used.add(hit)
        pairs.append((exp, hit))
    return pairs


def _format_checks(kind: str, expected: list, actual_steps: list) -> list:
    """One check per expected format, each consuming one actual step."""
    available = [
        canon_format(s.get("format")) for s in actual_steps if isinstance(s, dict)
    ]
    checks = []
    for want in expected:
        target = canon_format(want)
        if target in available:
            available.remove(target)
            checks.append(Check(f"{kind} format {target}", True))
        else:
            found = ", ".join(sorted(set(available))) or "none"
            checks.append(
                Check(
                    f"{kind} format {target}",
                    False,
                    f"not found, manifest has: {found}",
                )
            )
    return checks


def score_manifest(task: Task, manifest_toml: str, tools=None) -> Result:
    """Score one model answer. An empty manifest means the model built none.

    `tools` is the tool names the model called, when the caller recorded them.
    """
    text = manifest_toml or ""
    manifest = None
    parse_error = ""
    if text.strip():
        try:
            manifest = tomllib.loads(text)
        except Exception as e:
            parse_error = str(e)

    if task.unavailable:
        return Result(task.id, _unavailable_checks(task, manifest, tools), text)

    checks = [
        Check(
            "manifest parses",
            manifest is not None,
            parse_error or ("no manifest produced" if not text.strip() else ""),
        )
    ]
    graph = manifest or {}
    actual = topological_transforms(graph)

    checks += _format_checks("source", task.sources, graph.get("source") or [])

    pairs = _match_transforms(task.transforms, actual)
    for exp, hit in pairs:
        operation = exp.get("operation")
        checks.append(
            Check(
                f"operation {operation}",
                hit is not None,
                ""
                if hit is not None
                else "missing, manifest has: "
                + (
                    ", ".join(str(t.get("operation")) for t in actual)
                    or "no transforms"
                ),
            )
        )

    if len(task.transforms) > 1 and task.ordered:
        found = [hit for _, hit in pairs if hit is not None]
        ordered = found == sorted(found) and len(found) == len(task.transforms)
        checks.append(
            Check(
                "operation order",
                ordered,
                ""
                if ordered
                else "expected "
                + " -> ".join(str(e.get("operation")) for e in task.transforms)
                + ", manifest runs "
                + (" -> ".join(str(t.get("operation")) for t in actual) or "nothing"),
            )
        )

    for exp, hit in pairs:
        step = actual[hit] if hit is not None else {}
        tolerances = exp.get("tolerance") or {}
        for key, want in (exp.get("params") or {}).items():
            got = step.get(key)
            ok = got is not None and values_match(want, got, tolerances.get(key))
            checks.append(
                Check(
                    f"{exp.get('operation')}.{key} = {want}",
                    ok,
                    "" if ok else f"got {got!r}",
                )
            )

    checks += _format_checks("sink", task.sinks, graph.get("sink") or [])

    # without this, padding a manifest with spurious transforms costs nothing.
    # only for a manifest that parsed: no manifest at all must score zero, not
    # collect a point for the transforms it never wrote
    if manifest is not None:
        wanted = {str(e.get("operation", "")).strip().lower() for e in task.transforms}
        extra = [
            str(t.get("operation"))
            for t in actual
            if str(t.get("operation", "")).strip().lower() not in wanted
        ]
        checks.append(
            Check(
                "no unexpected operations",
                not extra,
                "" if not extra else f"also runs: {', '.join(extra)}",
            )
        )

    return Result(task.id, checks, text)


def _unavailable_checks(task: Task, manifest, tools) -> list:
    """A negative task must avoid the impossible manifest AND still do the work.

    Avoiding the manifest alone is not enough: a model that shrugs and does
    nothing would pass. When the caller knows which tools ran, the operation has
    to have been called directly instead. `tools` is None when scoring a captured
    manifest, where tool use was never recorded, so only the first check applies.
    """
    operations = [
        str(t.get("operation", "")).strip().lower()
        for t in (manifest or {}).get("transform") or []
        if isinstance(t, dict)
    ]
    target = str(task.unavailable).strip().lower()
    used = target in operations
    checks = [
        Check(
            f"avoids unavailable operation {target}",
            not used,
            "" if not used else f"built a manifest using {target}",
        )
    ]
    if tools is not None:
        called = [str(t).strip().lower() for t in tools]
        checks.append(
            Check(
                f"calls {target} directly instead",
                target in called,
                "" if target in called else f"never called {target}, so nothing ran",
            )
        )
    return checks


class TaskSamples:
    """One task run several times.

    Reads like a single `Result` so reports and aggregation do not care how many
    runs there were, but the score is the mean and the checks come from the worst
    run, so a task that only passes sometimes cannot report a lucky score.
    """

    def __init__(self, task_id: str, results: list):
        if not results:
            raise ValueError(f"task {task_id} has no runs to score")
        self.task_id = task_id
        self.results = list(results)

    @property
    def runs(self) -> int:
        return len(self.results)

    @property
    def worst(self):
        return min(self.results, key=lambda r: r.score)

    @property
    def score(self) -> float:
        return round(sum(r.score for r in self.results) / self.runs, 4)

    @property
    def low(self) -> float:
        return min(r.score for r in self.results)

    @property
    def high(self) -> float:
        return max(r.score for r in self.results)

    @property
    def flaky(self) -> bool:
        return self.low != self.high

    @property
    def passed(self) -> int:
        return self.worst.passed

    @property
    def total(self) -> int:
        return self.worst.total

    @property
    def failures(self) -> list:
        return self.worst.failures

    @property
    def manifest(self) -> str:
        return self.worst.manifest

    def as_dict(self) -> dict:
        return {
            "id": self.task_id,
            "score": self.score,
            "low": self.low,
            "high": self.high,
            "flaky": self.flaky,
            "runs": self.runs,
            "passed": self.passed,
            "total": self.total,
            "runs_detail": [r.as_dict() for r in self.results],
        }


def aggregate(results: list) -> dict:
    scores = [r.score for r in results]
    flaky = [r.task_id for r in results if getattr(r, "flaky", False)]
    return {
        "tasks": len(results),
        "score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "perfect": sum(1 for s in scores if s == 1.0),
        "checks_passed": sum(r.passed for r in results),
        "checks_total": sum(r.total for r in results),
        "runs_per_task": max((getattr(r, "runs", 1) for r in results), default=1),
        "flaky": flaky,
    }
