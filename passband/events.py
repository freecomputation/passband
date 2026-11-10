"""The event contract.

Every decoder runner writes events; the store keeps events; the
registry and the reports read events. This module is the one contract
they all answer to. An event is a plain JSON object with these fields:

- ts        UTC timestamp, ISO 8601, e.g. "2026-11-09T15:04:05Z"
- freq_hz   centre frequency the tuner was on, in Hz (number > 0)
- mode      what kind of hearing this was: "adsb", "sensor", "acars",
            "vdl2", "ais", "pocsag", "eas", "wxradio", "satellite",
            "ft8", "wspr", ...
- decoder   the runner that produced it, e.g. "readsb"
- edition   the edition the decoder or its data came from, e.g.
            "system" for a binary found on PATH, or "rtl433-data@2026-11"
- rssi_dbfs signal strength in dBFS where the decoder reports it, else null
- emitter   a stable key for the thing heard (aircraft hex, sensor
            model:id, ship MMSI, pager address), or null when the
            message carries no identity
- payload   a dict with everything else the decoder said

This module is not a decoder and not a store. It only defines the
shape and judges whether a candidate object matches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

TS_FORMAT_HINT = "YYYY-MM-DDTHH:MM:SSZ"
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")

REQUIRED_FIELDS = ("ts", "freq_hz", "mode", "decoder", "edition", "payload")


def now_iso() -> str:
    """Current UTC time in the event timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_at(moment: datetime) -> str:
    """Format an aware datetime in the event timestamp format."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    """Parse an event timestamp into an aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Event:
    """One thing heard, as a frozen record. Build via make_event()."""

    ts: str
    freq_hz: float
    mode: str
    decoder: str
    edition: str
    payload: dict
    rssi_dbfs: float | None = None
    emitter: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "freq_hz": self.freq_hz,
            "mode": self.mode,
            "decoder": self.decoder,
            "edition": self.edition,
            "rssi_dbfs": self.rssi_dbfs,
            "emitter": self.emitter,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, obj: dict) -> "Event":
        ok, reasons = validate_event(obj)
        if not ok:
            raise ValueError("invalid event: " + "; ".join(reasons))
        return cls(
            ts=obj["ts"],
            freq_hz=obj["freq_hz"],
            mode=obj["mode"],
            decoder=obj["decoder"],
            edition=obj["edition"],
            payload=obj["payload"],
            rssi_dbfs=obj.get("rssi_dbfs"),
            emitter=obj.get("emitter"),
        )


def make_event(
    mode: str,
    decoder: str,
    edition: str,
    freq_hz: float,
    payload: dict,
    ts: str | None = None,
    rssi_dbfs: float | None = None,
    emitter: str | None = None,
) -> Event:
    """Build and validate an Event; raises ValueError if malformed."""
    event = Event(
        ts=ts or now_iso(),
        freq_hz=freq_hz,
        mode=mode,
        decoder=decoder,
        edition=edition,
        payload=payload,
        rssi_dbfs=rssi_dbfs,
        emitter=emitter,
    )
    ok, reasons = validate_event(event.to_dict())
    if not ok:
        raise ValueError("invalid event: " + "; ".join(reasons))
    return event


def validate_event(obj) -> tuple[bool, list[str]]:
    """Judge a candidate event object.

    Returns (True, []) when it matches the contract, else
    (False, [reason, ...]) naming every problem found.
    """
    reasons: list[str] = []
    if not isinstance(obj, dict):
        return False, ["event is not a JSON object"]

    for name in REQUIRED_FIELDS:
        if name not in obj:
            reasons.append(f"missing field: {name}")

    ts = obj.get("ts")
    if ts is not None:
        if not isinstance(ts, str) or not _TS_RE.match(ts):
            reasons.append(f"ts is not an ISO 8601 UTC timestamp ({TS_FORMAT_HINT})")
        else:
            try:
                parse_ts(ts)
            except ValueError:
                reasons.append("ts does not parse as a real date/time")

    freq = obj.get("freq_hz")
    if freq is not None:
        if isinstance(freq, bool) or not isinstance(freq, (int, float)):
            reasons.append("freq_hz is not a number")
        elif freq <= 0:
            reasons.append("freq_hz must be > 0")

    for name in ("mode", "decoder", "edition"):
        value = obj.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            reasons.append(f"{name} must be a non-empty string")

    rssi = obj.get("rssi_dbfs")
    if rssi is not None and (isinstance(rssi, bool) or not isinstance(rssi, (int, float))):
        reasons.append("rssi_dbfs must be a number or null")

    emitter = obj.get("emitter")
    if emitter is not None and not isinstance(emitter, str):
        reasons.append("emitter must be a string or null")

    payload = obj.get("payload")
    if payload is not None and not isinstance(payload, dict):
        reasons.append("payload must be an object")

    return (not reasons), reasons
