"""The registry: the standing picture the events accumulate into.

Every event that names an emitter feeds the registry: what the thing
is (kind, from the event mode), when it was first and last heard, how
many times, on which days, and at which hours of the day it is usually
active. The registry is derived from the store - it is a query, not a
separate system, and it can be rebuilt at any time.

Its one judgement is expectation: an emitter that has been heard on at
least `min_days` of the comparable days before a date, inside its
usual hours, and is then silent on that date, is "unusually absent".
That is what lets the daily report say "the weather station that
reports every morning did not report today" without inventing
anything: the expectation is counted from the record itself.
"""

from __future__ import annotations

from datetime import date, timedelta

from .events import parse_ts


class EmitterRecord:
    """Accumulated picture of one emitter."""

    def __init__(self, key: str, kind: str):
        self.key = key
        self.kind = kind
        self.first_heard: str | None = None
        self.last_heard: str | None = None
        self.count = 0
        self.days: set[str] = set()
        self.hour_hist: list[int] = [0] * 24
        self.decoders: set[str] = set()

    def add(self, event: dict) -> None:
        ts = event["ts"]
        if self.first_heard is None or ts < self.first_heard:
            self.first_heard = ts
        if self.last_heard is None or ts > self.last_heard:
            self.last_heard = ts
        self.count += 1
        moment = parse_ts(ts)
        self.days.add(moment.strftime("%Y-%m-%d"))
        self.hour_hist[moment.hour] += 1
        self.decoders.add(event["decoder"])

    def usual_hours(self) -> list[int]:
        """Hours of the day holding at least 10% of this emitter's events."""
        if self.count == 0:
            return []
        threshold = max(1, self.count // 10)
        return [hour for hour, n in enumerate(self.hour_hist) if n >= threshold]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "kind": self.kind,
            "first_heard": self.first_heard,
            "last_heard": self.last_heard,
            "count": self.count,
            "days_heard": sorted(self.days),
            "hour_hist": list(self.hour_hist),
            "usual_hours": self.usual_hours(),
            "decoders": sorted(self.decoders),
        }


def build_registry(events: list[dict]) -> dict[str, EmitterRecord]:
    """Fold a list of events into emitter records, keyed by emitter."""
    registry: dict[str, EmitterRecord] = {}
    for event in events:
        key = event.get("emitter")
        if not key:
            continue
        record = registry.get(key)
        if record is None:
            record = EmitterRecord(key, event["mode"])
            registry[key] = record
        record.add(event)
    return registry


def missing_regulars(
    registry: dict[str, EmitterRecord],
    on_date: str,
    lookback_days: int = 7,
    min_days: int = 4,
) -> list[dict]:
    """Regulars expected on `on_date` (YYYY-MM-DD) but not heard.

    A regular is an emitter heard on at least `min_days` of the
    `lookback_days` days immediately before `on_date`. If it was heard
    on `on_date` at all, it is not missing. Returns plain dicts sorted
    by how established the regular is, strongest first.
    """
    target = date.fromisoformat(on_date)
    window = {
        (target - timedelta(days=offset)).isoformat()
        for offset in range(1, lookback_days + 1)
    }
    missing = []
    for record in registry.values():
        heard_in_window = len(record.days & window)
        if heard_in_window < min_days:
            continue
        if on_date in record.days:
            continue
        missing.append(
            {
                "key": record.key,
                "kind": record.kind,
                "days_in_window": heard_in_window,
                "window_days": lookback_days,
                "usual_hours": record.usual_hours(),
                "last_heard": record.last_heard,
                "total_count": record.count,
            }
        )
    missing.sort(key=lambda item: (-item["days_in_window"], item["key"]))
    return missing
