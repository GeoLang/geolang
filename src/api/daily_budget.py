from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)


def whole_number_limit(name: str, unit: str) -> int | None:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return None
    if not configured.isdecimal():
        raise RuntimeError(
            f"{name} must be a whole number of {unit}, got {configured!r}. "
            "Unset or 0 means no limit."
        )
    return int(configured) or None


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


# counts only hold while one process serves the API, a second worker counts on its own
class DailyBudget:
    def __init__(
        self,
        unit: str,
        limit_per_day: int | None,
        limit_per_caller_per_day: int | None,
        daily_spent_reply: str,
        caller_spent_reply: str,
        today: Callable[[], date] = utc_today,
    ):
        self.unit = unit
        self.limit_per_day = limit_per_day
        self.limit_per_caller_per_day = limit_per_caller_per_day
        self.daily_spent_reply = daily_spent_reply
        self.caller_spent_reply = caller_spent_reply
        self.today = today
        self.day: date | None = None
        self.spent_today = 0
        self.spent_today_by_caller: dict[str, int] = {}

    def start_new_day_if_due(self) -> None:
        today = self.today()
        if today != self.day:
            self.day = today
            self.spent_today = 0
            self.spent_today_by_caller = {}

    # None when the amount fits, otherwise the reply refusing it
    def refusal(self, caller: str | None, amount: int = 1) -> str | None:
        self.start_new_day_if_due()

        if self.limit_per_day is not None and self.spent_today + amount > self.limit_per_day:
            logger.warning(
                f"{self.unit} budget: {self.spent_today} of {self.limit_per_day} "
                f"spent today for all callers, refused {amount} more for caller {caller!r}"
            )
            return self.daily_spent_reply

        caller_limit = self.limit_per_caller_per_day
        if caller is None or caller_limit is None:
            return None

        caller_spent = self.spent_today_by_caller.get(caller, 0)
        if caller_spent + amount > caller_limit:
            logger.warning(
                f"{self.unit} budget: {caller_spent} of {caller_limit} spent today "
                f"for caller {caller!r}, refused {amount} more"
            )
            return self.caller_spent_reply
        return None

    def record(self, caller: str | None, amount: int = 1) -> None:
        self.start_new_day_if_due()
        self.spent_today += amount
        if caller is None or self.limit_per_caller_per_day is None:
            return
        self.spent_today_by_caller[caller] = self.spent_today_by_caller.get(caller, 0) + amount

    def spend(self, caller: str | None, amount: int = 1) -> str | None:
        refused = self.refusal(caller, amount)
        if refused is None:
            self.record(caller, amount)
        return refused
