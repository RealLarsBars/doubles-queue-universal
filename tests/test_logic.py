"""Tests for the parts of the bot that don't need Discord: time parsing, quiet hours and saving."""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["QUEUE_STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import queue_bot as qb  # noqa: E402

MON, TUE, FRI, SAT, SUN = 0, 1, 4, 5, 6


def at(day: int, hm: str) -> datetime:
    # 2026-10-19 is a Monday
    hour, minute = map(int, hm.split(":"))
    return datetime(2026, 10, 19 + day, hour, minute)


def test_parse_clock():
    assert qb.parse_clock("22:00") == "22:00"
    assert qb.parse_clock("9pm") == "21:00"
    assert qb.parse_clock("9:30 am") == "09:30"
    assert qb.parse_clock("12am") == "00:00"
    assert qb.parse_clock("12 PM") == "12:00"
    assert qb.parse_clock("0730") == "07:30"
    assert qb.parse_clock("7") == "07:00"
    assert qb.parse_clock("24:00") is None
    assert qb.parse_clock("24:00", allow_24=True) == "24:00"
    for bad in ("", "noon", "25:00", "13pm", "9:75"):
        assert qb.parse_clock(bad) is None, bad


def test_daytime_rule():
    rules = [{"days": [MON, TUE], "start": "09:00", "end": "17:00"}]
    assert qb.in_quiet_hours(rules, at(MON, "09:00"))
    assert qb.in_quiet_hours(rules, at(TUE, "16:59"))
    assert not qb.in_quiet_hours(rules, at(TUE, "17:00"))
    assert not qb.in_quiet_hours(rules, at(FRI, "12:00"))


def test_overnight_rule_runs_into_next_morning():
    rules = [{"days": [FRI], "start": "22:00", "end": "07:00"}]
    assert qb.in_quiet_hours(rules, at(FRI, "23:30"))
    assert qb.in_quiet_hours(rules, at(SAT, "06:59"))
    assert not qb.in_quiet_hours(rules, at(SAT, "07:00"))
    assert not qb.in_quiet_hours(rules, at(FRI, "06:00"))  # Thursday night isn't covered
    assert not qb.in_quiet_hours(rules, at(SAT, "23:00"))


def test_sunday_night_wraps_to_monday():
    rules = [{"days": [SUN], "start": "23:00", "end": "02:00"}]
    assert qb.in_quiet_hours(rules, at(MON, "01:00"))


def test_all_day_rule():
    rules = [{"days": [SAT], "start": "00:00", "end": "24:00"}]
    assert qb.in_quiet_hours(rules, at(SAT, "00:00"))
    assert qb.in_quiet_hours(rules, at(SAT, "23:59"))
    assert not qb.in_quiet_hours(rules, at(SUN, "00:00"))


def test_describe_rule():
    assert qb.describe_rule({"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "07:00"}) == \
        "Weekdays (Mon-Fri), 10:00 PM to 7:00 AM"
    assert qb.describe_rule({"days": [5], "start": "00:00", "end": "24:00"}) == "Sat, 12:00 AM to midnight"


def test_state_round_trip():
    g = qb.gs(123)
    g.ping_role_id = 55
    g.timezone = "America/Chicago"
    g.modes = {"Doubles": 4, "Singles": 2}
    g.queue("Doubles").append(qb.QueueEntry(7, "Player", 1.0, 2.0, prompted=True, prompt_message_id=9))
    g.quiet_hours[7] = [{"days": [0], "start": "09:00", "end": "17:00"}]
    g.quiet_muted.append(7)
    g.user_timezones[7] = "Europe/London"
    qb.save_state()

    loaded = qb.load_state()[123]
    assert loaded.ping_role_id == 55
    assert loaded.timezone == "America/Chicago"
    assert loaded.modes == {"Doubles": 4, "Singles": 2}
    assert loaded.queues["Doubles"][0].prompt_message_id == 9
    assert loaded.quiet_hours == {7: [{"days": [0], "start": "09:00", "end": "17:00"}]}
    assert loaded.quiet_muted == [7]
    assert loaded.user_timezones == {7: "Europe/London"}


def test_find_timezone():
    assert qb.find_timezone("America/Chicago") == "America/Chicago"
    assert qb.find_timezone("chicago") == "America/Chicago"
    assert qb.find_timezone("New York") == "America/New_York"
    assert qb.find_timezone("utc") == "UTC"
    assert qb.find_timezone("Atlantis") is None
    assert qb.find_timezone("  ") is None


def test_quiet_hours_use_the_players_own_timezone():
    g = qb.GuildState(timezone="UTC")
    g.quiet_hours[1] = [{"days": list(range(7)), "start": "00:00", "end": "24:00"}]
    assert qb.user_tz_name(g, 1) == "UTC"
    g.user_timezones[1] = "Asia/Tokyo"
    assert qb.user_tz_name(g, 1) == "Asia/Tokyo"
    assert qb.user_tz_name(g, 2) == "UTC"
    # A rule that is only quiet right now in the player's zone, not the server's
    from datetime import datetime, timedelta
    tokyo_now = datetime.now(qb.zone("Asia/Tokyo"))
    start = tokyo_now.strftime("%H:%M")
    end = (tokyo_now + timedelta(minutes=2)).strftime("%H:%M")
    if start < end:
        g.quiet_hours[1] = [{"days": [tokyo_now.weekday()], "start": start, "end": end}]
        assert qb.user_in_quiet_hours(g, 1)
        del g.user_timezones[1]
        assert not qb.user_in_quiet_hours(g, 1)
