"""The daily summary: how the owner reads the ear.

One report answers, for one day, from the record itself: how much was
heard and by which decoders; the busiest emitters; who was heard for
the first time; which regulars were unusually absent; how the
predicted satellite passes compared with what was heard; what went
dark while the tuner was elsewhere (from the scheduler's own grant
log, in the plan's own words); and any stale-data warnings, because a
prediction is only as good as the age of its elements.

The same summary renders as plain text for a terminal and as a single
static HTML file - no external assets, no scripts - that a browser,
including Coffer, can open from disk.

This module reads. It never listens, never writes to the store, and
invents nothing: every number in it is counted from events, grants,
or editions on disk.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import editions, triage
from .passes import ACCURACY_NOTE, find_passes, parse_tles
from .registry import build_registry, missing_regulars

STALE_TLE_DAYS = 14


def _day_bounds(day: str) -> tuple[str, str]:
    return f"{day}T00:00:00Z", f"{day}T23:59:59Z"


def _grants_for_day(data_dir, day: str) -> list[dict]:
    path = Path(data_dir) / "grants.jsonl"
    entries = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("ts", "")[:10] == day:
            entries.append(entry)
    return entries


def _tle_source(config: dict, data_dir):
    """(satellites, edition_date, label) for pass prediction, or (None,)*3."""
    text = None
    edition_date = None
    label = None
    tle_file = config.get("tle_file")
    if tle_file and Path(tle_file).is_file():
        text = Path(tle_file).read_text(encoding="utf-8", errors="replace")
        label = f"TLE file {tle_file}"
    else:
        edition_dir = editions.find_edition(data_dir, "tle", kind="reference")
        if edition_dir is not None:
            manifest = editions.load_manifest(edition_dir)
            edition_date = manifest.get("date")
            label = f"tle@{manifest.get('version')} edition"
            for candidate in sorted(Path(edition_dir).rglob("*.txt")):
                text = candidate.read_text(encoding="utf-8", errors="replace")
                break
        if text is None and (Path(data_dir) / "tle.txt").is_file():
            text = (Path(data_dir) / "tle.txt").read_text(
                encoding="utf-8", errors="replace"
            )
            label = "tle.txt in the data directory"
    if not text:
        return None, None, None
    return parse_tles(text), edition_date, label


def _triage_for_day(data_dir, day: str, day_events: list) -> dict | None:
    """Clip triage results for one day, from <data_dir>/triage.jsonl.

    The latest verdict per clip wins. A clip belongs to the day its
    transmission started (recorded in UTC in the clip's name), and
    voice clips are named with the label and frequency of the day's
    matching voice event when there is one. Returns None when no
    clip starting that day has ever been judged.
    """
    latest: dict[str, dict] = {}
    for record in triage.load_records(data_dir):
        if record.get("verdict") in ("voice", "static", "untriaged"):
            latest[record["clip"]] = record
    if not latest:
        return None
    by_clip = {}
    for event in day_events:
        payload = event.get("payload") or {}
        if payload.get("clip"):
            by_clip[payload["clip"]] = event
    counts = {"voice": 0, "static": 0, "untriaged": 0}
    voice_clips = []
    judged_that_day = 0
    for clip, record in latest.items():
        started = record.get("started_utc") or record.get("ts") or ""
        if started[:10] != day:
            continue
        judged_that_day += 1
        counts[record["verdict"]] += 1
        if record["verdict"] == "voice":
            event = by_clip.get(clip, {})
            payload = event.get("payload") or {}
            voice_clips.append({
                "clip": clip,
                "started_utc": record.get("started_utc"),
                "label": payload.get("label"),
                "freq_hz": event.get("freq_hz"),
                "transcript": (record.get("whisper") or {}).get(
                    "transcript", ""),
            })
    if not judged_that_day:
        return None
    voice_clips.sort(key=lambda item: item["started_utc"] or "")
    return {"counts": counts, "voice_clips": voice_clips}


def build_report(store, data_dir, day: str, config: dict | None = None) -> dict:
    """Count one day's report from the record. `day` is YYYY-MM-DD (UTC)."""
    config = config or {}
    since, until = _day_bounds(day)
    day_events = store.query(since=since, until=until)
    all_events = store.all_events()
    registry = build_registry(all_events)

    by_mode = Counter(event["mode"] for event in day_events)
    by_decoder = Counter(event["decoder"] for event in day_events)
    emitter_counts = Counter(
        event["emitter"] for event in day_events if event.get("emitter")
    )
    top_emitters = [
        {
            "key": key,
            "kind": registry[key].kind if key in registry else "?",
            "count": count,
        }
        for key, count in emitter_counts.most_common(10)
    ]
    new_emitters = sorted(
        record.key
        for record in registry.values()
        if record.first_heard and record.first_heard[:10] == day
    )
    missing = missing_regulars(registry, day)

    grants = _grants_for_day(data_dir, day)
    went_dark: list[str] = []
    for grant in grants:
        for item in grant.get("goes_dark", []):
            text = f"while {grant['decoder']} listened: {item}"
            if text not in went_dark:
                went_dark.append(text)
    refused = [g for g in grants if "refused" in g]

    satellites, tle_date, tle_label = _tle_source(config, data_dir)
    passes_info = None
    warnings: list[str] = []
    if satellites and config.get("lat") is not None and config.get("lon") is not None:
        start = datetime.combine(
            date.fromisoformat(day), time.min, tzinfo=timezone.utc
        )
        predicted = []
        for sat in satellites:
            predicted.extend(
                passage.to_dict()
                for passage in find_passes(
                    sat, float(config["lat"]), float(config["lon"]), start,
                    hours=24, alt_m=float(config.get("alt_m", 0)),
                )
            )
        heard = by_mode.get("satellite", 0)
        passes_info = {
            "predicted_count": len(predicted),
            "predicted": predicted,
            "heard_events": heard,
            "source": tle_label,
            "accuracy": ACCURACY_NOTE,
        }
        if tle_date:
            age = (date.fromisoformat(day) - date.fromisoformat(tle_date)).days
            if age > STALE_TLE_DAYS:
                warnings.append(
                    f"orbital elements are {age} days old (edition dated {tle_date}); "
                    "pass predictions degrade with element age"
                )
    elif satellites:
        warnings.append("orbital elements are aboard but no ground location is "
                        "configured, so no passes were predicted")

    for edition in editions.list_editions(data_dir):
        if not edition["ok"]:
            warnings.append(
                f"edition {edition.get('name')}@{edition.get('version')} fails "
                f"verification: {'; '.join(edition['problems'])}"
            )

    return {
        "date": day,
        "total_events": len(day_events),
        "by_mode": dict(by_mode),
        "by_decoder": dict(by_decoder),
        "top_emitters": top_emitters,
        "new_emitters": new_emitters,
        "missing_regulars": missing,
        "grants": grants,
        "grants_by_decoder": dict(Counter(g["decoder"] for g in grants)),
        "refused_grants": refused,
        "went_dark": went_dark,
        "passes": passes_info,
        "warnings": warnings,
        "triage": _triage_for_day(data_dir, day, day_events),
    }


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"Passband daily summary - {report['date']}")
    lines.append("=" * 44)
    lines.append(f"Events heard: {report['total_events']}")
    if report["by_mode"]:
        lines.append("")
        lines.append("By mode:")
        for mode, count in sorted(report["by_mode"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {mode}: {count}")
    if report["by_decoder"]:
        lines.append("")
        lines.append("By decoder:")
        for decoder, count in sorted(report["by_decoder"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {decoder}: {count}")
    if report["top_emitters"]:
        lines.append("")
        lines.append("Busiest emitters:")
        for item in report["top_emitters"]:
            lines.append(f"  {item['key']} ({item['kind']}): {item['count']}")
    lines.append("")
    lines.append("New emitters (first heard today):")
    if report["new_emitters"]:
        for key in report["new_emitters"]:
            lines.append(f"  {key}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("Unusually absent regulars:")
    if report["missing_regulars"]:
        for item in report["missing_regulars"]:
            hours = ", ".join(f"{h:02d}" for h in item["usual_hours"]) or "?"
            lines.append(
                f"  {item['key']} ({item['kind']}): heard {item['days_in_window']} of "
                f"{item['window_days']} prior days, usual hours {hours}, "
                f"last heard {item['last_heard']}"
            )
    else:
        lines.append("  none")
    if report.get("triage"):
        info = report["triage"]
        counts = info["counts"]
        lines.append("")
        lines.append("Airband clip triage:")
        lines.append(f"  voice: {counts['voice']}  static: {counts['static']}  "
                     f"untriaged: {counts['untriaged']}")
        for item in info["voice_clips"]:
            label = item["label"] or "airband"
            freq = (f"{item['freq_hz'] / 1e6:.3f} MHz"
                    if item.get("freq_hz") else "unknown frequency")
            lines.append(f"  voice clip: {label} ({freq}) "
                         f"{item['started_utc'] or ''}")
            if item["transcript"]:
                lines.append(f"    heard as: {item['transcript'][:100]}")
    lines.append("")
    lines.append("Satellite passes:")
    if report["passes"]:
        info = report["passes"]
        lines.append(f"  predicted: {info['predicted_count']} ({info['source']})")
        lines.append(f"  satellite events heard: {info['heard_events']}")
        lines.append(f"  {info['accuracy']}")
    else:
        lines.append("  not predicted - no elements aboard or no location configured")
    lines.append("")
    lines.append("What went dark (from the grant log):")
    if report["went_dark"]:
        for item in report["went_dark"]:
            lines.append(f"  {item}")
    else:
        lines.append("  nothing recorded")
    if report["refused_grants"]:
        lines.append("")
        lines.append("Refused grants:")
        for grant in report["refused_grants"]:
            lines.append(f"  {grant['decoder']}: {grant['refused']}")
    if report["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for warning in report["warnings"]:
            lines.append(f"  {warning}")
    return "\n".join(lines) + "\n"


def render_html(report: dict) -> str:
    """The same summary as one static HTML file. No external assets."""
    esc = html.escape
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Passband daily summary - {esc(report['date'])}</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:44em;margin:2em auto;"
        "padding:0 1em;line-height:1.45}h1{font-size:1.4em}h2{font-size:1.1em;"
        "margin-top:1.4em}table{border-collapse:collapse}td,th{border:1px solid #999;"
        "padding:.2em .6em;text-align:left}</style></head><body>",
        f"<h1>Passband daily summary - {esc(report['date'])}</h1>",
        f"<p>Events heard: <strong>{report['total_events']}</strong></p>",
    ]

    def table(title, rows):
        if not rows:
            return
        parts.append(f"<h2>{esc(title)}</h2><table>")
        for left, right in rows:
            parts.append(f"<tr><td>{esc(str(left))}</td><td>{esc(str(right))}</td></tr>")
        parts.append("</table>")

    table("By mode", sorted(report["by_mode"].items(), key=lambda kv: -kv[1]))
    table("By decoder", sorted(report["by_decoder"].items(), key=lambda kv: -kv[1]))
    table(
        "Busiest emitters",
        [(f"{i['key']} ({i['kind']})", i["count"]) for i in report["top_emitters"]],
    )
    parts.append("<h2>New emitters</h2>")
    parts.append(
        "<p>" + (", ".join(esc(k) for k in report["new_emitters"]) or "none") + "</p>"
    )
    parts.append("<h2>Unusually absent regulars</h2>")
    if report["missing_regulars"]:
        parts.append("<ul>")
        for item in report["missing_regulars"]:
            parts.append(
                f"<li>{esc(item['key'])} ({esc(item['kind'])}): heard "
                f"{item['days_in_window']} of {item['window_days']} prior days, "
                f"last heard {esc(str(item['last_heard']))}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>none</p>")
    if report.get("triage"):
        info = report["triage"]
        counts = info["counts"]
        parts.append("<h2>Airband clip triage</h2>")
        parts.append(
            f"<p>Voice: {counts['voice']} &middot; static: "
            f"{counts['static']} &middot; untriaged: "
            f"{counts['untriaged']}</p>")
        if info["voice_clips"]:
            parts.append("<ul>")
            for item in info["voice_clips"]:
                label = esc(str(item["label"] or "airband"))
                freq = (f"{item['freq_hz'] / 1e6:.3f} MHz"
                        if item.get("freq_hz") else "unknown frequency")
                parts.append(
                    f"<li>{label} ({esc(freq)}), "
                    f"{esc(str(item['started_utc'] or ''))}"
                    + (f" - heard as: {esc(item['transcript'][:100])}"
                       if item["transcript"] else "")
                    + "</li>")
            parts.append("</ul>")
    parts.append("<h2>Satellite passes</h2>")
    if report["passes"]:
        info = report["passes"]
        parts.append(
            f"<p>Predicted: {info['predicted_count']} ({esc(str(info['source']))}). "
            f"Satellite events heard: {info['heard_events']}. "
            f"{esc(info['accuracy'])}</p>"
        )
    else:
        parts.append("<p>Not predicted - no elements aboard or no location configured.</p>")
    parts.append("<h2>What went dark</h2>")
    if report["went_dark"]:
        parts.append("<ul>")
        for item in report["went_dark"]:
            parts.append(f"<li>{esc(item)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>nothing recorded</p>")
    if report["warnings"]:
        parts.append("<h2>Warnings</h2><ul>")
        for warning in report["warnings"]:
            parts.append(f"<li>{esc(warning)}</li>")
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
