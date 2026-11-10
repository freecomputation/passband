"""The event contract: valid events pass, malformed ones are refused
with reasons that name the problem."""

import pytest

from passband.events import Event, make_event, validate_event


def good_dict():
    return {
        "ts": "2026-11-09T15:04:05Z",
        "freq_hz": 1_090_000_000,
        "mode": "adsb",
        "decoder": "readsb",
        "edition": "system",
        "rssi_dbfs": -12.4,
        "emitter": "A1B2C3",
        "payload": {"flight": "DAL123"},
    }


def test_valid_event_passes():
    ok, reasons = validate_event(good_dict())
    assert ok, reasons


def test_roundtrip():
    event = Event.from_dict(good_dict())
    assert event.to_dict() == good_dict()
    assert event.emitter == "A1B2C3"


def test_nullable_fields_may_be_null():
    obj = good_dict()
    obj["rssi_dbfs"] = None
    obj["emitter"] = None
    ok, reasons = validate_event(obj)
    assert ok, reasons


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda o: o.pop("mode"), "mode"),
        (lambda o: o.pop("payload"), "payload"),
        (lambda o: o.update(ts="not a time"), "ts"),
        (lambda o: o.update(ts="2026-13-40T99:99:99Z"), "ts"),
        (lambda o: o.update(freq_hz=0), "freq_hz"),
        (lambda o: o.update(freq_hz=-5), "freq_hz"),
        (lambda o: o.update(freq_hz="1090"), "freq_hz"),
        (lambda o: o.update(payload=[1, 2]), "payload"),
        (lambda o: o.update(rssi_dbfs="loud"), "rssi_dbfs"),
        (lambda o: o.update(emitter=42), "emitter"),
        (lambda o: o.update(decoder=""), "decoder"),
    ],
)
def test_malformed_events_rejected_with_reasons(mutate, needle):
    obj = good_dict()
    mutate(obj)
    ok, reasons = validate_event(obj)
    assert not ok
    assert any(needle in reason for reason in reasons), reasons


def test_non_object_rejected():
    ok, reasons = validate_event([1, 2, 3])
    assert not ok and reasons


def test_make_event_stamps_now():
    event = make_event("adsb", "readsb", "system", 1_090_000_000, {})
    assert event.ts.endswith("Z")
    ok, _ = validate_event(event.to_dict())
    assert ok
