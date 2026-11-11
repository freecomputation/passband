"""The scheduler: one tuner, shared by time, on a published plan.

A plan is a JSON file: a name and a list of blocks. Each block says
which decoder listens, on what frequency, for how long at a time
(dwell_seconds), at what priority, inside what window of the local
day (start/end "HH:MM", or null for all day), and - honestly, in the
open - what goes dark while it runs. One tuner can be in one place at
a time; the plan is the manifest of that attention, and the owner
edits it.

The scheduler grants the tuner to one listener at a time and logs
every grant to <data_dir>/grants.jsonl: plan name, decoder, reason.
Satellite passes override the plan: when a predicted Meteor pass is
imminent or overhead, the tuner goes to SatDump for the pass and the
grant log says why ("pass-override"). Pass prediction is approximate
(see passband.passes) and the report repeats that.

Before a decoder runs, the editions gate is asked about it: a shelved
decoder edition that fails verification is never run, and the grant
log records the refusal. With no edition shelved, the decoder runs
from the system PATH and its events are stamped "system" - the record
says which world each event came from.

A decoder whose child dies the moment it starts - a bad config, a
busy tuner, a build missing its backend - is granted once, then
backed off for the rest of the run: its blocks are skipped like an
unavailable decoder's instead of being re-granted every second.

The loop is stoppable, and it is built to be tested: the clock and
the runner factory are injectable, and no child process is left
running when a grant ends.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import decoders as decoders_module
from . import editions
from . import tuner as tuner_module
from .events import iso_at
from .passes import ACCURACY_NOTE, find_passes, parse_tles

PASS_FREQ_HZ = 137_620_000
PASS_LEAD_SECONDS = 90

# A grant whose runner reports it never ran, with no events, inside
# this many seconds of starting is a child that died at startup.
# Real dwells are minutes long; nothing healthy ends this fast with
# nothing heard.
STARTUP_DEATH_SECONDS = 10.0


def _freq_text(freq_hz) -> str:
    hz = float(freq_hz or 0)
    if hz >= 1_000_000:
        return f"{hz / 1_000_000:g} MHz"
    if hz >= 1_000:
        return f"{hz / 1_000:g} kHz"
    return f"{hz:g} Hz"


def _event_line(event) -> str:
    """One short terminal line for one event heard.

    Who spoke (a flight, a hex, an emitter), then up to four payload
    facts in the order the decoder recorded them. Enough to watch
    the sky fill up; the store keeps the whole record.
    """
    stamp = (event.ts or "")[11:19] or "--:--:--"
    who = (
        event.payload.get("flight")
        or event.payload.get("hex")
        or event.emitter
        or ""
    )
    facts = []
    for key, value in event.payload.items():
        if key in ("flight", "hex") or value is None:
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            facts.append(f"{key} {value}")
        if len(facts) >= 4:
            break
    line = f"{stamp} {event.decoder} {event.mode}"
    if who:
        line += f" {who}"
    if facts:
        line += " - " + ", ".join(facts)
    return line


@dataclass
class PlanBlock:
    decoder: str
    freq_hz: int = 0
    dwell_seconds: int = 600
    priority: int = 1
    start: str | None = None
    end: str | None = None
    notes: str = ""
    goes_dark: list = field(default_factory=list)
    on: str = "plan"  # "plan" = normal rotation; "pass" = pass override only
    pipeline: str | None = None

    @classmethod
    def from_dict(cls, obj: dict) -> "PlanBlock":
        return cls(
            decoder=obj["decoder"],
            freq_hz=int(obj.get("freq_hz", 0)),
            dwell_seconds=int(obj.get("dwell_seconds", 600)),
            priority=int(obj.get("priority", 1)),
            start=obj.get("start"),
            end=obj.get("end"),
            notes=obj.get("notes", ""),
            goes_dark=list(obj.get("goes_dark", [])),
            on=obj.get("on", "plan"),
            pipeline=obj.get("pipeline"),
        )

    def active_at(self, moment: datetime) -> bool:
        if self.start is None and self.end is None:
            return True
        now = moment.strftime("%H:%M")
        if self.start is not None and self.end is not None:
            if self.start <= self.end:
                return self.start <= now < self.end
            return now >= self.start or now < self.end  # window wraps midnight
        if self.start is not None:
            return now >= self.start
        return now < self.end


@dataclass
class Plan:
    name: str
    description: str
    blocks: list

    @classmethod
    def from_dict(cls, obj: dict) -> "Plan":
        return cls(
            name=obj["name"],
            description=obj.get("description", ""),
            blocks=[PlanBlock.from_dict(block) for block in obj.get("blocks", [])],
        )


def load_plan(path) -> Plan:
    return Plan.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def plan_search_dirs(data_dir) -> list[Path]:
    repo_plans = Path(__file__).resolve().parent.parent / "plans"
    return [Path(data_dir) / "plans", repo_plans]


def find_plan(name: str, data_dir) -> Path:
    for directory in plan_search_dirs(data_dir):
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"plan {name!r} not found in {[str(d) for d in plan_search_dirs(data_dir)]}"
    )


def list_plans(data_dir) -> list[Plan]:
    found: dict[str, Plan] = {}
    for directory in reversed(plan_search_dirs(data_dir)):
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    plan = load_plan(path)
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
                found[plan.name] = plan
    return [found[name] for name in sorted(found)]


def make_pass_provider(config: dict, data_dir):
    """Build the pass-override provider from config + aboard elements.

    Returns a callable(now) -> Pass | None, or None when there is no
    ground location or no element set aboard - in which case the
    scheduler simply never overrides, and says so in its summary.
    """
    lat = config.get("lat")
    lon = config.get("lon")
    if lat is None or lon is None:
        return None
    tle_text = None
    tle_path = config.get("tle_file")
    if tle_path and Path(tle_path).is_file():
        tle_text = Path(tle_path).read_text(encoding="utf-8", errors="replace")
    else:
        edition_dir = editions.find_edition(data_dir, "tle", kind="reference")
        if edition_dir is not None:
            ok, _ = editions.verify(edition_dir)
            if ok:
                for candidate in sorted(Path(edition_dir).rglob("*.txt")):
                    tle_text = candidate.read_text(encoding="utf-8", errors="replace")
                    break
        if tle_text is None and (Path(data_dir) / "tle.txt").is_file():
            tle_text = (Path(data_dir) / "tle.txt").read_text(
                encoding="utf-8", errors="replace"
            )
    if not tle_text:
        return None
    satellites = [sat for sat in parse_tles(tle_text) if "METEOR" in sat.name.upper()]
    if not satellites:
        return None
    alt_m = float(config.get("alt_m", 0))

    def provider(now: datetime):
        window_start = now - timedelta(minutes=20)
        best = None
        for sat in satellites:
            for passage in find_passes(sat, float(lat), float(lon), window_start,
                                       hours=2.5, alt_m=alt_m):
                if passage.los <= now:
                    continue
                if best is None or passage.aos < best.aos:
                    best = passage
        return best

    return provider


class Scheduler:
    """Grants one tuner to one listener at a time, by plan and by pass."""

    def __init__(
        self,
        store,
        data_dir,
        simulate: bool = False,
        clock=None,
        sleeper=None,
        runner_factory=None,
        pass_provider=None,
        config: dict | None = None,
        announce=None,
    ):
        self.store = store
        self.data_dir = Path(data_dir)
        self.simulate = simulate
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleeper = sleeper or time.sleep
        self.runner_factory = runner_factory or decoders_module.get_runner
        self.config = config or {}
        self.announce = announce
        if pass_provider is None:
            pass_provider = make_pass_provider(self.config, self.data_dir)
        self.pass_provider = pass_provider
        self.stop_event = threading.Event()
        self.grants_path = self.data_dir / "grants.jsonl"
        self._last_block_key: str | None = None
        self._backed_off: set = set()
        self.summary = {"grants": 0, "events": 0, "refused": 0, "pass_overrides": 0,
                        "unavailable": 0}
        self._skipped_unavailable: list = []

    # -- grant bookkeeping ---------------------------------------------

    def _log_grant(self, entry: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self.grants_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def _emit(self, event) -> None:
        self.store.append(event)
        self.summary["events"] += 1
        self._say(_event_line(event))

    # -- the live terminal feed ------------------------------------------

    def _say(self, line: str) -> None:
        """One line to the person watching, if anyone is.

        The scheduler is silent by default (libraries, tests); the CLI
        passes a printer so a run in a terminal breathes out loud:
        grants, skips, refusals and one short line per event heard.
        A failing printer must never stop the listening.
        """
        if self.announce is None:
            return
        try:
            self.announce(line)
        except Exception:
            pass

    # -- choice of listener ----------------------------------------------

    def _block_available(self, block: PlanBlock) -> bool:
        """Can this block's runner actually run on this host right now?

        In simulate mode every runner replays its fixture and needs no
        binary, so every block is available. Outside simulate, the
        runner's own available() is the word - a missing binary, an
        unknown decoder, or a runner that errors while answering all
        mean the block cannot run and must not be granted.
        """
        if self.simulate:
            return True
        if block.decoder in self._backed_off:
            return False
        try:
            runner = self.runner_factory(block.decoder)
            available = getattr(runner, "available", None)
            if not callable(available):
                return True
            return bool(available())
        except Exception:
            return False

    def _log_unavailable(self, plan: Plan, block: PlanBlock, reason: str,
                         now: datetime) -> None:
        hint = ""
        try:
            status = self.runner_factory(block.decoder).status()
            hint = status.get("hint", "") if isinstance(status, dict) else ""
        except Exception:
            hint = ""
        entry = {
            "ts": iso_at(now),
            "plan": plan.name,
            "decoder": block.decoder,
            "freq_hz": block.freq_hz,
            "reason": reason,
            "goes_dark": block.goes_dark,
            "notes": block.notes,
            "unavailable": "decoder is not available on this host"
                           + (f" - {hint}" if hint else ""),
        }
        self._log_grant(entry)
        self.summary["unavailable"] += 1
        self._say(f"skip - {block.decoder} is not available on this host"
                  + (f" ({hint})" if hint else ""))

    def _choose_block(self, plan: Plan, now: datetime) -> PlanBlock | None:
        candidates = [
            block for block in plan.blocks
            if block.on == "plan" and block.active_at(now)
        ]
        self._skipped_unavailable = []
        if not candidates:
            return None
        runnable = []
        for block in candidates:
            if self._block_available(block):
                runnable.append(block)
            else:
                self._skipped_unavailable.append(block)
        if not runnable:
            return None
        candidates = runnable
        top_priority = max(block.priority for block in candidates)
        top = [block for block in candidates if block.priority == top_priority]
        if len(top) == 1:
            return top[0]
        keys = [f"{block.decoder}@{block.freq_hz}" for block in top]
        if self._last_block_key in keys:
            return top[(keys.index(self._last_block_key) + 1) % len(top)]
        return top[0]

    def _pass_block(self, plan: Plan, passage) -> PlanBlock:
        for block in plan.blocks:
            if block.decoder == "satdump" and block.on == "pass":
                return block
        pipeline = "metm2-4" if "M2 4" in passage.satellite.upper() else "metm2-3"
        return PlanBlock(
            decoder="satdump",
            freq_hz=PASS_FREQ_HZ,
            dwell_seconds=1200,
            priority=100,
            notes=f"satellite pass override ({ACCURACY_NOTE})",
            goes_dark=["all plan blocks"],
            on="pass",
            pipeline=pipeline,
        )

    # -- one grant ---------------------------------------------------------

    def _grant(self, plan: Plan, block: PlanBlock, reason: str,
               duration: float, now: datetime) -> None:
        stamp, ok, problems = editions.verify_named(
            self.data_dir, block.decoder, kind="decoder"
        )
        entry = {
            "ts": iso_at(now),
            "plan": plan.name,
            "decoder": block.decoder,
            "freq_hz": block.freq_hz,
            "reason": reason,
            "duration_seconds": duration,
            "goes_dark": block.goes_dark,
            "notes": block.notes,
            "edition": stamp,
        }
        if not ok:
            entry["refused"] = "edition failed verification: " + "; ".join(problems)
            self._log_grant(entry)
            self.summary["refused"] += 1
            self._say(f"refused - {block.decoder}: its shelved edition "
                      "failed verification")
            return
        self._log_grant(entry)
        self._say(f"listening - {block.decoder} at "
                  f"{_freq_text(block.freq_hz)} for {int(duration)}s ({reason})")
        runner = self.runner_factory(block.decoder)
        from .decoders import RunnerContext

        ctx = RunnerContext(
            data_dir=self.data_dir,
            edition=stamp,
            freq_hz=block.freq_hz or None,
            simulate=self.simulate,
            watch_dir=(Path(self.config["hf_watch_dir"])
                       if self.config.get("hf_watch_dir") else None),
            sat_pipeline=block.pipeline,
            airband_channels=self.config.get("airband_channels"),
            airband_live=bool(self.config.get("airband_live", False)),
            airband_squelch_snr=self.config.get("airband_squelch_snr"),
            airband_gain=self.config.get("airband_gain"),
            base_time=now if self.simulate else None,
        )
        stop = threading.Event()

        def watch_stop():
            while not stop.is_set():
                if self.stop_event.is_set():
                    stop.set()
                    break
                stop.wait(0.1)

        threading.Thread(target=watch_stop, daemon=True).start()
        result = None
        started = time.monotonic()
        try:
            result = runner.run(ctx, self._emit, stop, duration=duration)
        finally:
            stop.set()
        self.summary["grants"] += 1
        if (
            isinstance(result, dict)
            and result.get("ran") is False
            and not result.get("events")
            and time.monotonic() - started < STARTUP_DEATH_SECONDS
        ):
            # The child died at startup. Granting it again would spin
            # the same failure every second for the whole run, so the
            # decoder is backed off: from here on its blocks are
            # skipped like an unavailable decoder's.
            self._backed_off.add(block.decoder)
            self._say(f"backoff - {block.decoder} died at startup; "
                      "skipping it for the rest of this run")
        self._last_block_key = f"{block.decoder}@{block.freq_hz}"

    # -- the loop -----------------------------------------------------------

    def run(self, plan: Plan, once: bool = False,
            max_grants: int | None = None) -> dict:
        """Run the plan until stopped (or once, or max_grants grants)."""
        if not self.simulate:
            # A tuner holder this run did not start - an orphan from
            # a run that died hard, or the owner's own other session.
            # Name it and leave it alone: Passband never kills a
            # process it did not spawn. Grants that need the tuner
            # will fail against it, and the startup-death backoff
            # keeps that from spinning.
            for holder in tuner_module.tuner_holders():
                self._say(
                    f"tuner held - pid {holder['pid']}: "
                    f"{holder['command']} - not started by this run; "
                    "leaving it alone"
                )
        grants_done = 0
        while not self.stop_event.is_set():
            now = self.clock()
            passage = self.pass_provider(now) if self.pass_provider else None
            if passage is not None and passage.los > now and (
                passage.aos - now
            ).total_seconds() <= PASS_LEAD_SECONDS:
                block = self._pass_block(plan, passage)
                if not self._block_available(block):
                    self._log_unavailable(plan, block, "pass-override", now)
                    self.sleeper(30)
                    if once or (max_grants is not None and grants_done >= max_grants):
                        break
                    continue
                duration = max(1.0, (passage.los - now).total_seconds())
                duration = min(duration, 1800.0)
                self._grant(plan, block, "pass-override", duration, now)
                self.summary["pass_overrides"] += 1
                grants_done += 1
            else:
                block = self._choose_block(plan, now)
                for skipped in self._skipped_unavailable:
                    self._log_unavailable(plan, skipped, "plan", now)
                if block is None:
                    self.sleeper(30)
                    if once or (max_grants is not None and grants_done >= max_grants):
                        break
                    continue
                self._grant(plan, block, "plan", float(block.dwell_seconds), now)
                grants_done += 1
            if once or (max_grants is not None and grants_done >= max_grants):
                break
        self.summary["plan"] = plan.name
        self.summary["pass_prediction"] = ACCURACY_NOTE if self.pass_provider else None
        return self.summary
