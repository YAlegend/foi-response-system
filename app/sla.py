"""Statutory deadline (SLA) calculation in working days.

FOIA s.10 requires a response within 20 working days of receipt. Working days
exclude weekends and England & Wales bank holidays. The bank-holiday list below
is intentionally simple and should be replaced by a maintained source (e.g. the
GOV.UK bank-holidays JSON) in production — it is isolated here for that reason.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# England & Wales bank holidays. Extend / replace with a live feed in production.
ENGLAND_WALES_BANK_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4),
    date(2026, 5, 25), date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
    # 2027
    date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3),
    date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
}


def is_working_day(d: date, holidays: set[date] | None = None) -> bool:
    holidays = ENGLAND_WALES_BANK_HOLIDAYS if holidays is None else holidays
    return d.weekday() < 5 and d not in holidays


def add_working_days(start: date, n: int, holidays: set[date] | None = None) -> date:
    """Return the date that is `n` working days after `start`.

    The day of receipt does not count; counting begins the next working day,
    per the ICO's approach to s.10.
    """
    holidays = ENGLAND_WALES_BANK_HOLIDAYS if holidays is None else holidays
    d = start
    counted = 0
    while counted < n:
        d += timedelta(days=1)
        if is_working_day(d, holidays):
            counted += 1
    return d


def working_days_between(start: date, end: date, holidays: set[date] | None = None) -> int:
    """Count working days strictly after `start` up to and including `end`."""
    holidays = ENGLAND_WALES_BANK_HOLIDAYS if holidays is None else holidays
    if end <= start:
        return 0
    d = start
    count = 0
    while d < end:
        d += timedelta(days=1)
        if is_working_day(d, holidays):
            count += 1
    return count


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def deadline_for(received: date | datetime, working_days: int) -> date:
    return add_working_days(_as_date(received), working_days)


def sla_state(received: date | datetime, working_days: int, amber: int, red: int,
              today: date | None = None, paused_days: int = 0,
              paused_since: date | datetime | None = None) -> dict:
    """Return SLA status used by the dashboard and auto-escalation.

    The statutory clock can be paused while awaiting clarification (FOIA s.1(3)).
    ``paused_days`` is the working days already banked from past pauses; if
    ``paused_since`` is set the case is paused *now*, so the clock is frozen and
    the deadline keeps sliding out until clarification arrives.
    """
    received_d = _as_date(received)
    today = today or date.today()
    current_pause = working_days_between(_as_date(paused_since), today) if paused_since else 0
    total_paused = paused_days + current_pause

    elapsed = max(0, working_days_between(received_d, today) - total_paused)
    remaining = working_days - elapsed
    deadline = add_working_days(received_d, working_days + total_paused)
    if elapsed >= working_days:
        flag = "breach"
    elif elapsed >= red:
        flag = "red"
    elif elapsed >= amber:
        flag = "amber"
    else:
        flag = "green"
    return {
        "received": received_d.isoformat(),
        "deadline": deadline.isoformat(),
        "working_days_elapsed": elapsed,
        "working_days_remaining": remaining,
        "working_days_paused": total_paused,
        "paused": bool(paused_since),
        "flag": flag,
    }
