from __future__ import annotations

from collections.abc import Callable
from datetime import date

from src.api.daily_budget import DailyBudget, utc_today, whole_number_limit

CHAT_RUNS_PER_DAY_ENV = "GEOLANG_CHAT_RUNS_PER_DAY"
CHAT_RUNS_PER_CALLER_PER_DAY_ENV = "GEOLANG_CHAT_RUNS_PER_CALLER_PER_DAY"
CHAT_RUNS_UNIT = "chat runs per day"

DAILY_BUDGET_SPENT_REPLY = "The demo budget for today is used up. Try again tomorrow."
CALLER_BUDGET_SPENT_REPLY = "You have used today's chat budget. Try again tomorrow."


class ChatRunBudget(DailyBudget):
    def __init__(
        self,
        runs_per_day: int | None,
        runs_per_caller_per_day: int | None,
        today: Callable[[], date] = utc_today,
    ):
        super().__init__(
            "chat run",
            runs_per_day,
            runs_per_caller_per_day,
            DAILY_BUDGET_SPENT_REPLY,
            CALLER_BUDGET_SPENT_REPLY,
            today,
        )

    @classmethod
    def from_environment(cls) -> ChatRunBudget:
        return cls(
            whole_number_limit(CHAT_RUNS_PER_DAY_ENV, CHAT_RUNS_UNIT),
            whole_number_limit(CHAT_RUNS_PER_CALLER_PER_DAY_ENV, CHAT_RUNS_UNIT),
        )
