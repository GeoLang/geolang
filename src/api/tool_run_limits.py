from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date

from src.api.daily_budget import DailyBudget, utc_today, whole_number_limit
from src.api.upload_limits import megabyte_limit
from src.core import utils
from src.core.user_token import user_token_scope

TOOL_RUNS_PER_DAY_ENV = "GEOLANG_TOOL_RUNS_PER_DAY"
TOOL_RUNS_PER_CALLER_PER_DAY_ENV = "GEOLANG_TOOL_RUNS_PER_CALLER_PER_DAY"
TOOL_RUNS_AT_ONCE_PER_CALLER_ENV = "GEOLANG_TOOL_RUNS_AT_ONCE_PER_CALLER"
OUTPUT_MEGABYTES_PER_CALLER_PER_DAY_ENV = "GEOLANG_OUTPUT_MEGABYTES_PER_CALLER_PER_DAY"

RUNS_SPENT_TODAY_REPLY = "The tool run budget for today is used up. Try again tomorrow."
CALLER_RUNS_SPENT_REPLY = "You have used today's tool run budget. Try again tomorrow."
CALLER_OUTPUT_SPENT_REPLY = (
    "Your tool runs have written today's output size budget. Try again tomorrow."
)
RUNS_AT_ONCE_REPLY = (
    "You already have {limit} tool runs going, the most one person may have. "
    "Try again when one finishes."
)


class ToolRunRefused(Exception):
    pass


def directory_bytes(directory: str) -> int:
    total = 0
    for folder, _, names in os.walk(directory):
        for name in names:
            try:
                status = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            if stat.S_ISREG(status.st_mode):
                total += status.st_size
    return total


# anonymous is every ungated caller at once, so it gets only the global cap
def caller_of(token: str | None) -> str | None:
    with user_token_scope(token):
        directory = utils.current_caller_directory()
    return None if directory == utils.ANONYMOUS_OUTPUTS_DIRECTORY else directory


class ToolRunLimits:
    def __init__(
        self,
        runs_per_day: int | None,
        runs_per_caller_per_day: int | None,
        runs_at_once_per_caller: int | None,
        output_bytes_per_caller_per_day: int | None,
        today: Callable[[], date] = utc_today,
    ):
        self.run_budget = DailyBudget(
            "tool run",
            runs_per_day,
            runs_per_caller_per_day,
            RUNS_SPENT_TODAY_REPLY,
            CALLER_RUNS_SPENT_REPLY,
            today,
        )
        self.output_budget = DailyBudget(
            "tool output byte",
            None,
            output_bytes_per_caller_per_day,
            CALLER_OUTPUT_SPENT_REPLY,
            CALLER_OUTPUT_SPENT_REPLY,
            today,
        )
        self.runs_at_once_per_caller = runs_at_once_per_caller
        self.running_by_caller: dict[str, int] = {}
        # tool routes run in the threadpool
        self.lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> ToolRunLimits:
        return cls(
            whole_number_limit(TOOL_RUNS_PER_DAY_ENV, "tool runs per day"),
            whole_number_limit(TOOL_RUNS_PER_CALLER_PER_DAY_ENV, "tool runs per day"),
            whole_number_limit(TOOL_RUNS_AT_ONCE_PER_CALLER_ENV, "tool runs"),
            megabyte_limit(OUTPUT_MEGABYTES_PER_CALLER_PER_DAY_ENV, "megabytes per day"),
        )

    def start(self, caller: str | None) -> None:
        with self.lock:
            refused = self.run_budget.refusal(caller) or self.output_budget.refusal(
                caller, 0
            )
            if refused is not None:
                raise ToolRunRefused(refused)
            limit = self.runs_at_once_per_caller
            if caller is not None and limit is not None:
                running = self.running_by_caller.get(caller, 0)
                if running >= limit:
                    raise ToolRunRefused(RUNS_AT_ONCE_REPLY.format(limit=limit))
                self.running_by_caller[caller] = running + 1
            self.run_budget.record(caller)

    def finish(self, caller: str | None, written_bytes: int) -> None:
        with self.lock:
            self.output_budget.record(caller, written_bytes)
            if caller is None or self.runs_at_once_per_caller is None:
                return
            self.running_by_caller[caller] -= 1
            if not self.running_by_caller[caller]:
                del self.running_by_caller[caller]

    def measured_outputs(self, caller: str | None) -> str | None:
        if caller is None or self.output_budget.limit_per_caller_per_day is None:
            return None
        return os.path.join(utils.OUTPUTS_ROOT, caller)

    @contextmanager
    def run(self, token: str | None) -> Iterator[None]:
        caller = caller_of(token)
        self.start(caller)
        outputs = self.measured_outputs(caller)
        before = directory_bytes(outputs) if outputs else 0
        try:
            yield
        finally:
            written = max(0, directory_bytes(outputs) - before) if outputs else 0
            self.finish(caller, written)
