"""The event store: the product of Passband.

Events live as one JSON object per line in dated files under
<data_dir>/events/YYYY-MM-DD.jsonl. Those files are append-only and
they are the truth: they survive this program, and any tool that can
read text lines can read them.

Alongside them the store grows a sqlite index (stdlib sqlite3) so
queries are fast. The index is derived and disposable: delete
index.sqlite3 and it is rebuilt from the JSONL on next use. Nothing
exists only in the index.

This module is not a query language and not a server. It appends,
it rebuilds, it answers plain questions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .events import Event, parse_ts, validate_event

INDEX_NAME = "index.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    freq_hz REAL,
    mode TEXT,
    decoder TEXT,
    edition TEXT,
    rssi_dbfs REAL,
    emitter TEXT,
    payload_json TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_mode ON events(mode);
CREATE INDEX IF NOT EXISTS idx_events_emitter ON events(emitter);
"""


class EventStore:
    """Append-only JSONL event files plus a derived sqlite index."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.events_dir = self.data_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.data_dir / INDEX_NAME
        self._conn: sqlite3.Connection | None = None

    # -- writing -----------------------------------------------------

    def day_file(self, day: str) -> Path:
        return self.events_dir / f"{day}.jsonl"

    def append(self, event: Event) -> None:
        """Append one validated event to its day file and the index."""
        ok, reasons = validate_event(event.to_dict())
        if not ok:
            raise ValueError("refusing to store invalid event: " + "; ".join(reasons))
        # Connect first: a fresh index is loaded from the JSONL as it
        # exists *before* this line lands, or the line would be counted
        # twice (once by the load, once by the insert below).
        conn = self._connect()
        day = event.ts[:10]
        line = json.dumps(event.to_dict(), sort_keys=True)
        with open(self.day_file(day), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        conn.execute(
            "INSERT INTO events (ts, day, freq_hz, mode, decoder, edition,"
            " rssi_dbfs, emitter, payload_json, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.ts,
                day,
                event.freq_hz,
                event.mode,
                event.decoder,
                event.edition,
                event.rssi_dbfs,
                event.emitter,
                json.dumps(event.payload, sort_keys=True),
                line,
            ),
        )
        conn.commit()

    def append_dict(self, obj: dict) -> Event:
        event = Event.from_dict(obj)
        self.append(event)
        return event

    # -- reading -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            fresh = not self.index_path.exists()
            self._conn = sqlite3.connect(str(self.index_path))
            self._conn.executescript(_SCHEMA)
            if fresh:
                self._load_all_into_index(self._conn)
        return self._conn

    def _load_all_into_index(self, conn: sqlite3.Connection) -> int:
        count = 0
        for path in sorted(self.events_dir.glob("*.jsonl")):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ok, _ = validate_event(obj)
                    if not ok:
                        continue
                    conn.execute(
                        "INSERT INTO events (ts, day, freq_hz, mode, decoder,"
                        " edition, rssi_dbfs, emitter, payload_json, raw_json)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            obj["ts"],
                            obj["ts"][:10],
                            obj["freq_hz"],
                            obj["mode"],
                            obj["decoder"],
                            obj["edition"],
                            obj.get("rssi_dbfs"),
                            obj.get("emitter"),
                            json.dumps(obj["payload"], sort_keys=True),
                            line,
                        ),
                    )
                    count += 1
        conn.commit()
        return count

    def rebuild_index(self) -> int:
        """Throw the index away and regrow it from the JSONL truth."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self.index_path.exists():
            self.index_path.unlink()
        conn = self._connect()
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])

    def query(
        self,
        since: str | None = None,
        until: str | None = None,
        mode: str | None = None,
        decoder: str | None = None,
        emitter: str | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Answer a plain question over the store, oldest first.

        since/until are ISO timestamps (inclusive). text is a free-text
        substring matched against the payload JSON.
        """
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        if mode is not None:
            clauses.append("mode = ?")
            params.append(mode)
        if decoder is not None:
            clauses.append("decoder = ?")
            params.append(decoder)
        if emitter is not None:
            clauses.append("emitter = ?")
            params.append(emitter)
        if text is not None:
            clauses.append("payload_json LIKE ?")
            params.append(f"%{text}%")
        sql = "SELECT raw_json FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        conn = self._connect()
        rows = conn.execute(sql, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def all_events(self) -> list[dict]:
        return self.query()

    def days(self) -> list[str]:
        """The days that have event files, sorted."""
        return sorted(p.stem for p in self.events_dir.glob("*.jsonl"))

    def count(self) -> int:
        conn = self._connect()
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
