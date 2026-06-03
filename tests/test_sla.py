from datetime import date

from app.sla import add_working_days, deadline_for, sla_state, working_days_between


def test_add_working_days_skips_weekend():
    # Thursday 4 June 2026 + 2 working days -> Monday 8 June 2026
    assert add_working_days(date(2026, 6, 4), 2) == date(2026, 6, 8)


def test_add_working_days_skips_bank_holiday():
    # Friday 22 May 2026 + 1 working day skips Mon 25 May (bank holiday) -> Tue 26 May
    assert add_working_days(date(2026, 5, 22), 1) == date(2026, 5, 26)


def test_deadline_is_20_working_days():
    received = date(2026, 6, 1)  # Monday
    deadline = deadline_for(received, 20)
    assert working_days_between(received, deadline) == 20


def test_sla_flags():
    received = date(2026, 6, 1)
    green = sla_state(received, 20, 12, 17, today=date(2026, 6, 5))
    assert green["flag"] == "green"
    amber = sla_state(received, 20, 12, 17, today=date(2026, 6, 19))
    assert amber["flag"] in {"amber", "red"}
    breach = sla_state(received, 20, 12, 17, today=date(2026, 8, 1))
    assert breach["flag"] == "breach"


def test_sla_banked_pause_reduces_elapsed_and_pushes_deadline():
    received = date(2026, 6, 1)
    base = sla_state(received, 20, 12, 17, today=date(2026, 6, 15))
    paused = sla_state(received, 20, 12, 17, today=date(2026, 6, 15), paused_days=3)
    assert paused["working_days_elapsed"] == base["working_days_elapsed"] - 3
    assert paused["working_days_remaining"] == base["working_days_remaining"] + 3
    assert paused["deadline"] > base["deadline"]      # later ISO date string
    assert paused["paused"] is False                  # no *current* pause


def test_sla_active_pause_freezes_the_clock():
    received = date(2026, 6, 1)
    # Paused since 8 June: elapsed should only count working days up to the pause.
    s = sla_state(received, 20, 12, 17, today=date(2026, 6, 22), paused_since=date(2026, 6, 8))
    assert s["paused"] is True
    assert s["working_days_elapsed"] == working_days_between(received, date(2026, 6, 8))
