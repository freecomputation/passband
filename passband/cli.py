"""The command line: how a person runs the ear.

    passband doctor                     dongle, driver, decoder binaries
    passband plan list | show NAME      the published plans of attention
    passband run --plan NAME [--once] [--simulate]
    passband listen DECODER [--seconds N] [--simulate]
    passband capture --freq HZ [--seconds N] [--simulate]
    passband events [--mode M] [--since TS] [--emitter K] [--limit N]
    passband registry
    passband passes [--hours 24]
    passband report [--date YYYY-MM-DD] [--html PATH]
    passband edition shelve|verify|list
    passband compost
    passband triage [--compost] [--days N] [--limit N]

Everything runs locally against a data directory (default
~/.local/share/passband, override with --data-dir). Ground location and
tuning for passes/snippets live in <data_dir>/config.json:
{"lat": 45.0, "lon": -100.0, "alt_m": 500, "tle_file": null,
 "retention_days": 7, "cap_gib": 2, "hf_watch_dir": null,
 "triage_model": "small", "triage_retention_days": 7}.

--simulate runs the whole stack on a simulated tuner: decoders replay
their recorded fixtures through the real parsers, events land in the
real store, and reports read them back. Nothing simulated is quiet
about being simulated.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from . import __version__, decoders as decoders_module, editions, snippets, triage
from .events import iso_at
from .passes import ACCURACY_NOTE, find_passes, parse_tles
from .registry import build_registry
from .report import build_report, render_html, render_text
from .scheduler import Scheduler, find_plan, list_plans, load_plan
from .store import EventStore
from .tuner import RtlSdrTuner, SimulatedTuner, detect_dongle, driver_status

def _default_data_dir() -> Path:
    current = Path.home() / ".local" / "share" / "passband"
    if current.exists():
        return current
    # Stores kept under the previous name stay in use until the
    # owner moves them; the events and clips in them are the record.
    legacy = Path.home() / ".local" / "share" / "siuti"
    if legacy.exists():
        return legacy
    return current


DEFAULT_DATA_DIR = _default_data_dir()


def load_config(data_dir: Path) -> dict:
    path = Path(data_dir) / "config.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"warning: {path} is not valid JSON; ignoring it", file=sys.stderr)
    return {}


def _store(args) -> EventStore:
    return EventStore(args.data_dir)


# -- doctor ------------------------------------------------------------


def cmd_doctor(args) -> int:
    print(f"passband {__version__}")
    info = detect_dongle()
    if info.get("found"):
        print(f"dongle: {info['model']}")
        if info.get("device"):
            print(f"  device: {info['device']}")
        if info.get("serial"):
            print(f"  serial: {info['serial']}")
        if info.get("tuner_line"):
            print(f"  {info['tuner_line']}")
    else:
        print(f"dongle: not detected - {info.get('reason')}")
    driver = driver_status()
    print(f"driver: librtlsdr = {driver['librtlsdr']}, blog fork = {driver['blog_fork']}")
    print(f"  note: {driver['note']}")
    print("decoders:")
    for status in decoders_module.runner_statuses():
        mark = "ok " if status["available"] else "-- "
        line = f"  {mark}{status['runner']:<10} ({status['mode']})"
        if status.get("binary"):
            line += f" binary: {status['binary']}"
        if status.get("kind"):
            line += f" [{status['kind']}]"
        print(line)
        if not status["available"] and status.get("hint"):
            print(f"       install: {status['hint']}")
        if status["runner"] == "wxradio":
            print(f"       SAME/EAS decoding via multimon-ng: "
                  f"{'available' if status.get('same_decoding') else 'not installed'}")
    return 0


# -- plans ---------------------------------------------------------------


def cmd_plan(args) -> int:
    if args.action == "list":
        for plan in list_plans(args.data_dir):
            print(f"{plan.name}: {plan.description}")
            for block in plan.blocks:
                window = (
                    "all day" if block.start is None and block.end is None
                    else f"{block.start or '..'} - {block.end or '..'}"
                )
                print(f"  {block.decoder:<10} {block.freq_hz:>12} Hz  "
                      f"dwell {block.dwell_seconds}s  prio {block.priority}  {window}"
                      f"{'  [pass override only]' if block.on == 'pass' else ''}")
        return 0
    path = find_plan(args.name, args.data_dir)
    print(path.read_text(encoding="utf-8"))
    return 0


# -- run / listen ----------------------------------------------------------


def cmd_run(args) -> int:
    store = _store(args)
    config = load_config(args.data_dir)
    plan = load_plan(find_plan(args.plan, args.data_dir))
    announce = None
    if not args.quiet:
        def announce(line):
            print(line, flush=True)
    scheduler = Scheduler(
        store,
        args.data_dir,
        simulate=args.simulate,
        config=config,
        announce=announce,
    )
    if args.simulate:
        print("simulate mode: decoders replay recorded fixtures; "
              "events are stamped simulated", file=sys.stderr)
    try:
        summary = scheduler.run(plan, once=args.once)
    except KeyboardInterrupt:
        scheduler.stop_event.set()
        summary = scheduler.summary
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_listen(args) -> int:
    store = _store(args)
    config = load_config(args.data_dir)
    runner = decoders_module.get_runner(args.decoder)
    stamp, ok, problems = editions.verify_named(
        args.data_dir, args.decoder, kind="decoder"
    )
    if not ok:
        print(f"refusing to run {args.decoder}: its shelved edition failed "
              f"verification: {'; '.join(problems)}", file=sys.stderr)
        return 1
    from .decoders import RunnerContext

    ctx = RunnerContext(
        data_dir=Path(args.data_dir),
        edition=stamp,
        freq_hz=args.freq,
        simulate=args.simulate,
        watch_dir=Path(config["hf_watch_dir"]) if config.get("hf_watch_dir") else None,
        airband_channels=config.get("airband_channels"),
        airband_live=bool(config.get("airband_live", False)),
        airband_squelch_snr=config.get("airband_squelch_snr"),
        airband_gain=config.get("airband_gain"),
        base_time=datetime.now(timezone.utc) if args.simulate else None,
    )

    def emit(event):
        store.append(event)
        print(json.dumps(event.to_dict(), sort_keys=True))

    result = runner.run(ctx, emit, threading.Event(), duration=args.seconds)
    print(json.dumps(result, sort_keys=True), file=sys.stderr)
    return 0 if result.get("ran") else 1


# -- capture / compost -------------------------------------------------------


def cmd_capture(args) -> int:
    tuner = SimulatedTuner() if args.simulate else RtlSdrTuner()
    if not args.simulate and not tuner.available():
        print("rtl_sdr not found on PATH - the RTL-SDR tools are not installed",
              file=sys.stderr)
        return 1
    data_path, meta_path = snippets.capture_snippet(
        tuner, args.data_dir, args.freq, args.seconds
    )
    print(f"data: {data_path} ({data_path.stat().st_size} bytes)")
    print(f"meta: {meta_path}")
    return 0


def cmd_compost(args) -> int:
    config = load_config(args.data_dir)
    days = args.days if args.days is not None else int(config.get("retention_days", 7))
    cap = args.cap_gib if args.cap_gib is not None else float(config.get("cap_gib", 2))
    actions = snippets.compost(args.data_dir, retention_days=days, cap_gib=cap)
    if not actions:
        print("nothing to compost")
    for action in actions:
        print(f"composted {action['file']} ({action['bytes']} bytes): {action['reason']}")
    return 0


def cmd_triage(args) -> int:
    config = load_config(args.data_dir)
    summary = triage.triage_data_dir(
        args.data_dir, config, limit=args.limit, force=args.force)
    print(f"triage: {summary['voice']} voice, {summary['static']} static, "
          f"{summary['untriaged']} untriaged, {summary['skipped']} "
          f"already judged")
    if summary.get("blocked"):
        print(f"triage blocked: {summary['blocked']}", file=sys.stderr)
        return 2
    if args.compost:
        days = (args.days if args.days is not None
                else int(config.get("triage_retention_days",
                                    triage.DEFAULT_RETENTION_DAYS)))
        actions = triage.compost_static(args.data_dir, days)
        if not actions:
            print("no static clips past the retention age")
    return 0


# -- events / registry ---------------------------------------------------------


def cmd_events(args) -> int:
    store = _store(args)
    events = store.query(
        since=args.since, until=args.until, mode=args.mode,
        decoder=args.decoder, emitter=args.emitter, text=args.text,
        limit=args.limit,
    )
    for event in events:
        print(json.dumps(event, sort_keys=True))
    print(f"# {len(events)} event(s)", file=sys.stderr)
    return 0


def cmd_registry(args) -> int:
    store = _store(args)
    registry = build_registry(store.all_events())
    records = sorted(registry.values(), key=lambda r: -r.count)
    if not records:
        print("registry is empty - nothing with an identity has been heard yet")
        return 0
    for record in records:
        hours = ",".join(f"{h:02d}" for h in record.usual_hours()) or "-"
        print(f"{record.key}  ({record.kind})  heard {record.count}x  "
              f"first {record.first_heard}  last {record.last_heard}  "
              f"usual hours {hours}")
    return 0


# -- passes ----------------------------------------------------------------------


def cmd_passes(args) -> int:
    from .report import _tle_source

    config = load_config(args.data_dir)
    if config.get("lat") is None or config.get("lon") is None:
        print("no ground location configured: create "
              f"{Path(args.data_dir) / 'config.json'} with "
              '{"lat": ..., "lon": ...} first', file=sys.stderr)
        return 2
    satellites, tle_date, label = _tle_source(config, args.data_dir)
    if not satellites:
        print("no orbital elements aboard: shelve a tle reference edition, "
              "set tle_file in config.json, or place tle.txt in the data directory",
              file=sys.stderr)
        return 2
    print(f"elements: {label}" + (f" (dated {tle_date})" if tle_date else ""))
    print(ACCURACY_NOTE)
    now = datetime.now(timezone.utc)
    found = 0
    for sat in satellites:
        for passage in find_passes(
            sat, float(config["lat"]), float(config["lon"]), now,
            hours=args.hours, alt_m=float(config.get("alt_m", 0)),
        ):
            found += 1
            print(f"{passage.satellite}: AOS {iso_at(passage.aos)}  "
                  f"TCA {iso_at(passage.tca)}  LOS {iso_at(passage.los)}  "
                  f"max {passage.max_el_deg:.0f} deg")
    if not found:
        print(f"no passes above 10 deg in the next {args.hours} h")
    return 0


# -- report -------------------------------------------------------------------------


def cmd_report(args) -> int:
    store = _store(args)
    config = load_config(args.data_dir)
    day = args.date or date.today().isoformat()
    report = build_report(store, args.data_dir, day, config)
    print(render_text(report), end="")
    if args.html:
        Path(args.html).write_text(render_html(report), encoding="utf-8")
        print(f"html written to {args.html}", file=sys.stderr)
    return 0


# -- editions -------------------------------------------------------------------------


def cmd_edition(args) -> int:
    if args.action == "shelve":
        target = editions.shelve(
            args.src, args.data_dir, name=args.name, kind=args.kind,
            version=args.version, source=args.source, need=args.need,
            edition_date=args.date,
        )
        print(f"shelved {args.name}@{args.version} at {target}")
        ok, problems = editions.verify(target)
        print("verify: " + ("ok" if ok else "FAILED - " + "; ".join(problems)))
        return 0 if ok else 1
    if args.action == "list":
        found = editions.list_editions(args.data_dir)
        if not found:
            print("no editions shelved")
        for edition in found:
            print(f"{edition.get('name')}@{edition.get('version')} "
                  f"({edition.get('kind')}, dated {edition.get('date')}, "
                  f"{edition.get('files')} files) - "
                  f"{'sealed ok' if edition['ok'] else 'BROKEN: ' + '; '.join(edition['problems'])}")
        return 0
    # verify: NAME or NAME@VERSION
    if "@" in args.name:
        name, version = args.name.split("@", 1)
        target = editions.edition_path(args.data_dir, name, version)
    else:
        target = editions.find_edition(args.data_dir, args.name)
        if target is None:
            print(f"no edition shelved under the name {args.name!r}", file=sys.stderr)
            return 1
    ok, problems = editions.verify(target)
    if ok:
        print(f"{target.name}: sealed ok")
        return 0
    print(f"{target.name}: FAILED verification: {'; '.join(problems)}", file=sys.stderr)
    return 1


# -- parser ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="passband",
        description="Passband - the ear of Fixity. Receive-only, offline-first SDR listening.",
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="data directory (default ~/.local/share/siuti)")
    parser.add_argument("--version", action="version", version=f"passband {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="dongle, driver and decoder presence")

    plan_parser = sub.add_parser("plan", help="inspect day plans")
    plan_sub = plan_parser.add_subparsers(dest="action", required=True)
    plan_sub.add_parser("list")
    show = plan_sub.add_parser("show")
    show.add_argument("name")

    run = sub.add_parser("run", help="run a day plan under the scheduler")
    run.add_argument("--plan", default="weekday")
    run.add_argument("--once", action="store_true", help="one grant, then exit")
    run.add_argument("--simulate", action="store_true")
    run.add_argument("--quiet", action="store_true",
                     help="no live terminal lines; only the closing summary")

    listen = sub.add_parser("listen", help="run one decoder directly")
    listen.add_argument("decoder", choices=sorted(decoders_module.RUNNERS))
    listen.add_argument("--seconds", type=float, default=60)
    listen.add_argument("--freq", type=int, default=None)
    listen.add_argument("--simulate", action="store_true")

    capture = sub.add_parser("capture", help="capture a SigMF snippet")
    capture.add_argument("--freq", type=int, required=True)
    capture.add_argument("--seconds", type=float, default=5)
    capture.add_argument("--simulate", action="store_true")

    events_parser = sub.add_parser("events", help="query the event store")
    events_parser.add_argument("--mode")
    events_parser.add_argument("--decoder")
    events_parser.add_argument("--emitter")
    events_parser.add_argument("--since")
    events_parser.add_argument("--until")
    events_parser.add_argument("--text")
    events_parser.add_argument("--limit", type=int)

    sub.add_parser("registry", help="the emitter registry")

    passes_parser = sub.add_parser("passes", help="predicted satellite passes")
    passes_parser.add_argument("--hours", type=float, default=24)

    report_parser = sub.add_parser("report", help="daily summary")
    report_parser.add_argument("--date")
    report_parser.add_argument("--html")

    edition_parser = sub.add_parser("edition", help="the editions gate")
    edition_sub = edition_parser.add_subparsers(dest="action", required=True)
    shelve = edition_sub.add_parser("shelve")
    shelve.add_argument("--src", required=True)
    shelve.add_argument("--name", required=True)
    shelve.add_argument("--kind", required=True, choices=list(editions.KINDS))
    shelve.add_argument("--version", required=True)
    shelve.add_argument("--source", required=True)
    shelve.add_argument("--need", required=True)
    shelve.add_argument("--date")
    verify = edition_sub.add_parser("verify")
    verify.add_argument("name", help="NAME or NAME@VERSION")
    edition_sub.add_parser("list")

    compost_parser = sub.add_parser("compost", help="compost old snippets")
    compost_parser.add_argument("--days", type=int)
    compost_parser.add_argument("--cap-gib", type=float)

    triage_parser = sub.add_parser(
        "triage", help="judge airband clips: voice, static, or untriaged")
    triage_parser.add_argument("--limit", type=int,
                               help="judge at most this many new clips")
    triage_parser.add_argument("--force", action="store_true",
                               help="re-judge clips already on record")
    triage_parser.add_argument("--compost", action="store_true",
                               help="also compost static clips past the "
                                    "retention age")
    triage_parser.add_argument("--days", type=int,
                               help="retention age in days for --compost "
                                    "(default: config triage_retention_days "
                                    "or 7)")
    return parser


_COMMANDS = {
    "doctor": cmd_doctor,
    "plan": cmd_plan,
    "run": cmd_run,
    "listen": cmd_listen,
    "capture": cmd_capture,
    "events": cmd_events,
    "registry": cmd_registry,
    "passes": cmd_passes,
    "report": cmd_report,
    "edition": cmd_edition,
    "compost": cmd_compost,
    "triage": cmd_triage,
}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.data_dir = Path(args.data_dir)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
