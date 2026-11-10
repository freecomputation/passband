"""The store: JSONL is the truth, the index is derived and disposable."""

import json

from passband.events import make_event
from passband.store import EventStore


def ev(mode="adsb", emitter="A1B2C3", ts="2026-11-09T10:00:00Z", payload=None):
    return make_event(
        mode, "readsb", "system", 1_090_000_000, payload or {"x": 1},
        ts=ts, emitter=emitter,
    )


def test_append_and_count(store):
    store.append(ev())
    store.append(ev(emitter="ABC123", ts="2026-11-09T10:01:00Z"))
    assert store.count() == 2
    assert store.days() == ["2026-11-09"]


def test_jsonl_file_is_plain_json_lines(store, data_dir):
    store.append(ev())
    lines = (data_dir / "events" / "2026-11-09.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["emitter"] == "A1B2C3"


def test_query_filters(store):
    store.append(ev(mode="adsb", emitter="A1B2C3", payload={"flight": "DAL123"}))
    store.append(ev(mode="sensor", emitter="WX:1", ts="2026-11-09T11:00:00Z"))
    store.append(ev(mode="adsb", emitter="ABC123", ts="2026-11-09T12:00:00Z"))
    assert len(store.query(mode="adsb")) == 2
    assert len(store.query(emitter="WX:1")) == 1
    assert len(store.query(since="2026-11-09T11:30:00Z")) == 1
    assert len(store.query(until="2026-11-09T10:30:00Z")) == 1
    assert len(store.query(text="DAL123")) == 1
    assert len(store.query(limit=2)) == 2
    assert store.query(mode="ais") == []


def test_fresh_store_does_not_double_count_first_append(data_dir):
    """Regression: the first append on a fresh store must land once,
    even though a fresh index is grown from the JSONL at connect."""
    store = EventStore(data_dir)
    store.append(ev())
    store.close()
    reopened = EventStore(data_dir)
    assert reopened.count() == 1


def test_index_rebuild_from_jsonl(store, data_dir):
    store.append(ev())
    store.append(ev(emitter="ABC123", ts="2026-11-09T10:05:00Z"))
    store.close()
    (data_dir / "index.sqlite3").unlink()
    fresh = EventStore(data_dir)
    assert fresh.rebuild_index() == 2
    assert fresh.count() == 2
    assert len(fresh.query(emitter="ABC123")) == 1


def test_rebuild_after_manual_index_delete_and_reopen(data_dir):
    store = EventStore(data_dir)
    store.append(ev())
    store.close()
    (data_dir / "index.sqlite3").unlink()
    again = EventStore(data_dir)
    assert again.count() == 1  # regrown at connect, without rebuild_index()
