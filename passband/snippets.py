"""Triggered captures: the evidence, kept briefly.

Passband does not archive raw spectrum - at 2.4 MS/s that is 0.41 TB a
day of mostly nothing. What it keeps on demand is a snippet: a few
seconds of raw I/Q around something interesting, written as a SigMF
pair (.sigmf-data + .sigmf-meta) under <data_dir>/snippets/, so the
moment can be re-examined with any SigMF tool.

Snippets are composted, not kept forever: anything older than the
retention days is deleted, and if the snippet store still exceeds
its size cap the oldest go first, until both bounds hold. Defaults
are 7 days and 2 GiB; both are the owner's to change. Every compost
action is logged to <data_dir>/compost.jsonl.

The data itself is exactly what rtl_sdr emits: unsigned 8-bit
interleaved I/Q, SigMF datatype "cu8_le".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import iso_at, now_iso
from .tuner import SAMPLE_RATE_DEFAULT

DEFAULT_RETENTION_DAYS = 7
DEFAULT_CAP_GIB = 2.0
SIGMF_VERSION = "1.0.0"
SIGMF_DATATYPE = "cu8_le"


def snippets_dir(data_dir) -> Path:
    path = Path(data_dir) / "snippets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_sigmf(
    data_dir,
    raw: bytes,
    freq_hz: int,
    sample_rate: int = SAMPLE_RATE_DEFAULT,
    when: datetime | None = None,
    hardware: str = "RTL-SDR (RTL2832U family)",
) -> tuple[Path, Path]:
    """Write one SigMF pair for raw cu8 bytes. Returns (data, meta) paths."""
    moment = when or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    base = snippets_dir(data_dir) / f"{stamp}_{int(freq_hz)}"
    data_path = base.with_suffix(".sigmf-data")
    meta_path = base.with_suffix(".sigmf-meta")
    counter = 1
    while data_path.exists():
        data_path = snippets_dir(data_dir) / f"{stamp}_{int(freq_hz)}_{counter}.sigmf-data"
        meta_path = data_path.with_suffix(".sigmf-meta")
        counter += 1
    data_path.write_bytes(raw)
    meta = {
        "global": {
            "core:version": SIGMF_VERSION,
            "core:datatype": SIGMF_DATATYPE,
            "core:sample_rate": sample_rate,
            "core:hw": hardware,
            "core:description": "Passband triggered capture - receive only, kept as evidence",
        },
        "captures": [
            {
                "core:sample_start": 0,
                "core:frequency": int(freq_hz),
                "core:datetime": iso_at(moment),
            }
        ],
        "annotations": [],
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return data_path, meta_path


def capture_snippet(
    tuner,
    data_dir,
    freq_hz: int,
    seconds: float,
    sample_rate: int = SAMPLE_RATE_DEFAULT,
    gain: float | None = None,
) -> tuple[Path, Path]:
    """Pull `seconds` of raw spectrum through the tuner seam and shelve it."""
    raw = tuner.capture_bytes(freq_hz, seconds, sample_rate=sample_rate, gain=gain)
    hardware = "simulated tuner (no hardware)" if getattr(tuner, "simulated", False) else "RTL-SDR (RTL2832U family)"
    return write_sigmf(data_dir, raw, freq_hz, sample_rate=sample_rate, hardware=hardware)


def _snippet_pairs(data_dir) -> list[tuple[float, int, Path, Path]]:
    """(mtime, size_bytes, data_path, meta_path) for every snippet, oldest first."""
    pairs = []
    for data_path in snippets_dir(data_dir).glob("*.sigmf-data"):
        meta_path = data_path.with_suffix(".sigmf-meta")
        size = data_path.stat().st_size
        if meta_path.exists():
            size += meta_path.stat().st_size
        pairs.append((data_path.stat().st_mtime, size, data_path, meta_path))
    pairs.sort()
    return pairs


def compost(
    data_dir,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    cap_gib: float = DEFAULT_CAP_GIB,
    now: datetime | None = None,
) -> list[dict]:
    """Delete snippets past the age bound, then past the size cap.

    Oldest first in both passes. Returns the list of actions taken and
    appends them to <data_dir>/compost.jsonl.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=retention_days)).timestamp()
    cap_bytes = int(cap_gib * 1024**3)
    pairs = _snippet_pairs(data_dir)
    actions: list[dict] = []

    def drop(entry, reason):
        _, size, data_path, meta_path = entry
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        actions.append(
            {
                "ts": now_iso(),
                "action": "compost",
                "file": data_path.name,
                "bytes": size,
                "reason": reason,
            }
        )

    kept = []
    for entry in pairs:
        if entry[0] < cutoff:
            drop(entry, f"older than {retention_days} days")
        else:
            kept.append(entry)
    total = sum(entry[1] for entry in kept)
    index = 0
    while total > cap_bytes and index < len(kept):
        total -= kept[index][1]
        drop(kept[index], f"snippet store over {cap_gib} GiB cap")
        index += 1

    if actions:
        log_path = Path(data_dir) / "compost.jsonl"
        with open(log_path, "a", encoding="utf-8") as handle:
            for action in actions:
                handle.write(json.dumps(action, sort_keys=True) + "\n")
    return actions
