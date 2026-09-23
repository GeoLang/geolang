from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

CHAT_RUNS_PER_DAY_ENV = "GEOLANG_CHAT_RUNS_PER_DAY"
CHAT_RUNS_PER_CALLER_PER_DAY_ENV = "GEOLANG_CHAT_RUNS_PER_CALLER_PER_DAY"

DAILY_BUDGET_SPENT_REPLY = "The demo budget for today is used up. Try again tomorrow."
CALLER_BUDGET_SPENT_REPLY = "You have used today's chat budget. Try again tomorrow."


def daily_run_limit(name: str) -> int | None:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return None
    if not configured.isdecimal():
        raise RuntimeError(
            f"{name} must be a whole number of chat runs per day, got "
            f"{configured!r}. Unset or 0 means no limit."
        )
    return int(configured) or None


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


# counts only hold while one process serves the API, a second worker counts on its own
class ChatRunBudget:
    def __init__(
        self,
        runs_per_day: int | None,
        runs_per_caller_per_day: int | None,
        today: Callable[[], date] = utc_today,
    ):
        self.runs_per_day = runs_per_day
        self.runs_per_caller_per_day = runs_per_caller_per_day
        self.today = today
        self.day: date | None = None
        self.runs_today = 0
        self.runs_today_by_caller: dict[str, int] = {}

    @classmethod
    def from_environment(cls) -> ChatRunBudget:
        return cls(
            daily_run_limit(CHAT_RUNS_PER_DAY_ENV),
            daily_run_limit(CHAT_RUNS_PER_CALLER_PER_DAY_ENV),
        )

    # None when the run is counted, otherwise the reply refusing it
    def count_run(self, caller: str | None) -> str | None:
        today = self.today()
        if today != self.day:
            self.day = today
            self.runs_today = 0
            self.runs_today_by_caller = {}

        if self.runs_per_day is not None and self.runs_today >= self.runs_per_day:
            logger.warning(
                f"chat budget: {self.runs_today} of {self.runs_per_day} runs "
                f"today for all callers, refused caller {caller!r}"
            )
            return DAILY_BUDGET_SPENT_REPLY

        caller_limit = self.runs_per_caller_per_day
        if caller is None or caller_limit is None:
            self.runs_today += 1
            return None

        caller_runs = self.runs_today_by_caller.get(caller, 0)
        if caller_runs >= caller_limit:
            logger.warning(
                f"chat budget: {caller_runs} of {caller_limit} runs today for "
                f"caller {caller!r}, refused"
            )
            return CALLER_BUDGET_SPENT_REPLY

        self.runs_today += 1
        self.runs_today_by_caller[caller] = caller_runs + 1
        return None
