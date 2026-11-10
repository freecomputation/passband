"""The registry: accumulation over days, and the unusually-absent
judgement the daily report is built on."""

from passband.events import make_event
from passband.registry import build_registry, missing_regulars

DAYS = [f"2026-11-0{d}" for d in range(2, 9)]  # 02..08, seven days


def heard(emitter, day, hour=8, mode="sensor"):
    return make_event(
        mode, "rtl_433", "system", 433_920_000, {},
        ts=f"{day}T{hour:02d}:15:00Z", emitter=emitter,
    ).to_dict()


def test_accumulation():
    events = [heard("WX:1", DAYS[0], hour=8), heard("WX:1", DAYS[0], hour=9),
              heard("WX:1", DAYS[1], hour=8)]
    registry = build_registry(events)
    record = registry["WX:1"]
    assert record.count == 3
    assert record.first_heard == f"{DAYS[0]}T08:15:00Z"
    assert record.last_heard == f"{DAYS[1]}T08:15:00Z"
    assert record.days == {DAYS[0], DAYS[1]}
    assert 8 in record.usual_hours()


def test_events_without_emitter_are_skipped():
    anonymous = make_event("wxradio", "wxradio", "system", 162_550_000, {},
                           ts="2026-11-09T08:00:00Z").to_dict()
    assert build_registry([anonymous]) == {}


def test_missing_regular_detected():
    events = []
    for day in DAYS:  # heard every one of the 7 prior days
        events.append(heard("WX:1", day))
    for day in DAYS[:2]:  # an irregular, heard only twice
        events.append(heard("TPMS:9", day, hour=17))
    registry = build_registry(events)
    missing = missing_regulars(registry, "2026-11-09")
    keys = [item["key"] for item in missing]
    assert "WX:1" in keys
    assert "TPMS:9" not in keys
    entry = next(item for item in missing if item["key"] == "WX:1")
    assert entry["days_in_window"] == 7
    assert 8 in entry["usual_hours"]


def test_present_regular_is_not_missing():
    events = [heard("WX:1", day) for day in DAYS]
    events.append(heard("WX:1", "2026-11-09"))
    registry = build_registry(events)
    assert missing_regulars(registry, "2026-11-09") == []


def test_min_days_threshold_respected():
    events = [heard("WX:1", day) for day in DAYS[:3]]  # only 3 of 7
    registry = build_registry(events)
    assert missing_regulars(registry, "2026-11-09", min_days=4) == []
    assert missing_regulars(registry, "2026-11-09", min_days=3) != []
