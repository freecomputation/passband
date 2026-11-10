"""Snippets: SigMF pairs written through the tuner seam, and the
compost that keeps them inside their age and size bounds."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

from passband import snippets
from passband.tuner import SimulatedTuner


def test_capture_writes_valid_sigmf(data_dir):
    tuner = SimulatedTuner()
    data_path, meta_path = snippets.capture_snippet(
        tuner, data_dir, 137_620_000, seconds=0.01
    )
    assert data_path.exists() and meta_path.exists()
    assert data_path.stat().st_size == int(2_400_000 * 0.01) * 2
    meta = json.loads(meta_path.read_text())
    assert meta["global"]["core:datatype"] == "cu8_le"
    assert meta["global"]["core:sample_rate"] == 2_400_000
    assert meta["captures"][0]["core:frequency"] == 137_620_000
    assert "simulated" in meta["global"]["core:hw"]


def _make_snippet(data_dir, name_time, size, age_days=0):
    raw = b"\x7f" * size
    when = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc) - timedelta(days=age_days)
    data_path, meta_path = snippets.write_sigmf(
        data_dir, raw, 100_000_000, when=when
    )
    stamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    os.utime(data_path, (stamp, stamp))
    os.utime(meta_path, (stamp, stamp))
    return data_path


def test_compost_by_age(data_dir):
    _make_snippet(data_dir, None, 100, age_days=10)
    fresh = _make_snippet(data_dir, None, 100, age_days=0)
    now = datetime.now(timezone.utc)
    actions = snippets.compost(data_dir, retention_days=7, cap_gib=2, now=now)
    assert len(actions) == 1
    assert actions[0]["reason"].startswith("older than")
    remaining = list(snippets.snippets_dir(data_dir).glob("*.sigmf-data"))
    assert [path.name for path in remaining] == [fresh.name]
    log = (data_dir / "compost.jsonl").read_text().strip().splitlines()
    assert len(log) == 1


def test_compost_by_cap_oldest_first(data_dir):
    base = time.time()
    paths = []
    for index in range(3):
        path = _make_snippet(data_dir, None, 2000, age_days=0)
        stamp = base - (3 - index) * 100  # oldest first: index 0
        os.utime(path, (stamp, stamp))
        meta = path.with_suffix(".sigmf-meta")
        os.utime(meta, (stamp, stamp))
        paths.append(path)
    cap_gib = 4500 / 1024**3  # ~4500 bytes: room for roughly one pair
    actions = snippets.compost(
        data_dir, retention_days=365, cap_gib=cap_gib,
        now=datetime.now(timezone.utc),
    )
    assert len(actions) == 2
    assert all("cap" in action["reason"] for action in actions)
    remaining = list(snippets.snippets_dir(data_dir).glob("*.sigmf-data"))
    assert [path.name for path in remaining] == [paths[-1].name]  # newest survives


def test_compost_nothing_to_do(data_dir):
    assert snippets.compost(data_dir) == []
