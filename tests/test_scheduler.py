"""The scheduler: rotation by plan, pass preemption, edition refusal -
all with a fake clock and fake runners, so the sequencing is proven
without hardware or real time passing."""

import json
from datetime import datetime, timedelta, timezone

from passband import editions
from passband.events import make_event
from passband.passes import Pass
from passband.scheduler import Plan, Scheduler

START = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeRunner:
    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    def available(self):
        return True

    def status(self):
        return {"runner": self.name, "available": True, "hint": ""}

    def run(self, ctx, emit, stop_event, duration=None):
        self.calls.append((self.name, duration))
        emit(
            make_event("test", self.name, ctx.edition, ctx.freq_hz or 1000,
                       {"fake": True}, ts="2026-11-09T12:00:00Z")
        )
        return {"runner": self.name, "ran": True, "events": 1}


def make_scheduler(store, data_dir, clock, calls, **kwargs):
    return Scheduler(
        store,
        data_dir,
        clock=clock,
        sleeper=clock.advance,
        runner_factory=lambda name: FakeRunner(name, calls),
        **kwargs,
    )


def plan_two_blocks():
    return Plan.from_dict(
        {
            "name": "test-plan",
            "blocks": [
                {"decoder": "readsb", "freq_hz": 1090000000, "dwell_seconds": 60,
                 "priority": 5, "goes_dark": ["sensors"]},
                {"decoder": "rtl_433", "freq_hz": 433920000, "dwell_seconds": 60,
                 "priority": 5, "goes_dark": ["ads-b"]},
            ],
        }
    )


def grants(data_dir):
    path = data_dir / "grants.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_rotation_between_equal_priority_blocks(store, data_dir):
    clock = FakeClock()
    calls = []
    scheduler = make_scheduler(store, data_dir, clock, calls)
    summary = scheduler.run(plan_two_blocks(), max_grants=3)
    assert [name for name, _ in calls] == ["readsb", "rtl_433", "readsb"]
    assert summary["grants"] == 3
    assert summary["events"] == 3
    logged = grants(data_dir)
    assert len(logged) == 3
    assert all(entry["reason"] == "plan" for entry in logged)
    assert logged[0]["plan"] == "test-plan"
    assert logged[0]["goes_dark"] == ["sensors"]
    assert store.count() == 3


def test_higher_priority_wins(store, data_dir):
    clock = FakeClock()
    calls = []
    plan = Plan.from_dict(
        {
            "name": "prio",
            "blocks": [
                {"decoder": "rtl_433", "freq_hz": 433920000, "dwell_seconds": 60,
                 "priority": 1},
                {"decoder": "readsb", "freq_hz": 1090000000, "dwell_seconds": 60,
                 "priority": 9},
            ],
        }
    )
    scheduler = make_scheduler(store, data_dir, clock, calls)
    scheduler.run(plan, max_grants=2)
    assert [name for name, _ in calls] == ["readsb", "readsb"]


def test_inactive_window_means_no_grant(store, data_dir):
    clock = FakeClock()  # 12:00 UTC
    calls = []
    plan = Plan.from_dict(
        {
            "name": "night-only",
            "blocks": [
                {"decoder": "hf", "freq_hz": 14074000, "dwell_seconds": 60,
                 "priority": 5, "start": "20:00", "end": "06:00"},
            ],
        }
    )
    scheduler = make_scheduler(store, data_dir, clock, calls)
    summary = scheduler.run(plan, once=True)
    assert summary["grants"] == 0
    assert calls == []


def test_pass_override_preempts(store, data_dir):
    clock = FakeClock()
    calls = []
    passage = Pass(
        satellite="METEOR-M2 3",
        aos=START + timedelta(seconds=30),
        los=START + timedelta(seconds=630),
        tca=START + timedelta(seconds=330),
        max_el_deg=42.0,
    )
    scheduler = make_scheduler(
        store, data_dir, clock, calls, pass_provider=lambda now: passage
    )
    summary = scheduler.run(plan_two_blocks(), once=True)
    assert calls and calls[0][0] == "satdump"
    assert calls[0][1] == 630  # the rest of the pass, not a plan dwell
    assert summary["pass_overrides"] == 1
    logged = grants(data_dir)
    assert logged[0]["reason"] == "pass-override"
    assert logged[0]["decoder"] == "satdump"


def test_tampered_decoder_edition_is_refused(store, data_dir, tmp_path):
    clock = FakeClock()
    calls = []
    src = tmp_path / "src"
    src.mkdir()
    (src / "decoder.bin").write_text("pretend decoder", encoding="utf-8")
    target = editions.shelve(
        src, data_dir, name="readsb", kind="decoder", version="1",
        source="test", need="test refusal",
    )
    (target / "decoder.bin").write_text("tampered", encoding="utf-8")
    scheduler = make_scheduler(store, data_dir, clock, calls)
    summary = scheduler.run(plan_two_blocks(), once=True)
    assert summary["refused"] == 1
    assert calls == []  # the refused decoder never ran
    assert "edition failed verification" in grants(data_dir)[0]["refused"]


def test_verified_edition_stamps_events(store, data_dir, tmp_path):
    clock = FakeClock()
    calls = []
    src = tmp_path / "src"
    src.mkdir()
    (src / "decoder.bin").write_text("pretend decoder", encoding="utf-8")
    editions.shelve(
        src, data_dir, name="readsb", kind="decoder", version="2",
        source="test", need="test stamp",
    )
    plan = Plan.from_dict(
        {"name": "one", "blocks": [
            {"decoder": "readsb", "freq_hz": 1090000000, "dwell_seconds": 60,
             "priority": 5}]}
    )
    scheduler = make_scheduler(store, data_dir, clock, calls)
    scheduler.run(plan, once=True)
    events = store.all_events()
    assert events and events[0]["edition"] == "readsb@2"


class UnavailableRunner(FakeRunner):
    """A runner whose binary is not on this host: never runs, says so."""

    def available(self):
        return False

    def status(self):
        return {"runner": self.name, "available": False,
                "hint": f"install {self.name} first"}

    def run(self, ctx, emit, stop_event, duration=None):
        raise AssertionError("an unavailable runner must never be granted")


def make_mixed_scheduler(store, data_dir, clock, calls, unavailable_names, **kwargs):
    def factory(name):
        if name in unavailable_names:
            return UnavailableRunner(name, calls)
        return FakeRunner(name, calls)

    return Scheduler(
        store,
        data_dir,
        clock=clock,
        sleeper=clock.advance,
        runner_factory=factory,
        **kwargs,
    )


def plan_top_and_lower():
    return Plan.from_dict(
        {
            "name": "mixed",
            "blocks": [
                {"decoder": "readsb", "freq_hz": 1090000000, "dwell_seconds": 60,
                 "priority": 10, "goes_dark": ["sensors"]},
                {"decoder": "rtl_433", "freq_hz": 433920000, "dwell_seconds": 60,
                 "priority": 5, "goes_dark": ["ads-b"]},
            ],
        }
    )


def test_top_priority_unavailable_falls_through(store, data_dir):
    clock = FakeClock()
    calls = []
    scheduler = make_mixed_scheduler(store, data_dir, clock, calls, {"readsb"})
    summary = scheduler.run(plan_top_and_lower(), once=True)
    assert [name for name, _ in calls] == ["rtl_433"]
    assert summary["grants"] == 1
    assert summary["unavailable"] == 1
    logged = grants(data_dir)
    unavailable_entries = [e for e in logged if "unavailable" in e]
    assert len(unavailable_entries) == 1
    assert unavailable_entries[0]["decoder"] == "readsb"
    assert "install readsb first" in unavailable_entries[0]["unavailable"]


def test_all_unavailable_grants_nothing_and_sleeps(store, data_dir):
    clock = FakeClock()
    calls = []
    scheduler = make_mixed_scheduler(
        store, data_dir, clock, calls, {"readsb", "rtl_433"}
    )
    summary = scheduler.run(plan_top_and_lower(), once=True)
    assert summary["grants"] == 0
    assert calls == []
    assert summary["unavailable"] == 2
    # the loop took the sleep path exactly once, then honoured once=True
    assert clock.now == START + timedelta(seconds=30)
    logged = grants(data_dir)
    assert len(logged) == 2
    assert all("unavailable" in entry for entry in logged)


def test_live_output_announces_grants_skips_and_events(store, data_dir):
    clock = FakeClock()
    calls = []
    lines = []
    scheduler = make_mixed_scheduler(
        store, data_dir, clock, calls, {"readsb"}, announce=lines.append
    )
    scheduler.run(plan_top_and_lower(), once=True)
    assert any(line.startswith("skip - readsb") for line in lines)
    assert any(line.startswith("listening - rtl_433 at 433.92 MHz")
               for line in lines)
    assert any(" rtl_433 test " in f" {line} " for line in lines)
