"""Decoder runners: thin supervision around other people's decoders.

Passband invents no decoders. The free software world maintains them -
readsb, rtl_433, dumpvdl2, acarsdec, AIS-catcher, multimon-ng,
SatDump, WSJT-X - and this module runs them: builds their command
lines, reads what they print, turns it into events that answer to
passband.events, and stops them cleanly. A decoder never touches the
store; a runner validates and stamps every event it emits with the
decoder's name and the edition it came from.

Every runner answers honestly when its binary is not installed:
status() says unavailable and names the install hint, run() returns
without emitting, and nothing anywhere crashes because a decoder is
missing. A missing decoder is a fact about the machine, not an error
in Passband.

When the tuner is simulated, runners replay their recorded fixture
(fixtures/ directory) through the very same parsers, restamped to the
present, so the whole stack can be seen working with no hardware and
no decoder binaries at all.

This module contains no network code. The one file-shaped exception
is readsb, which Passband runs with --write-json to a local directory
and polls the aircraft.json file it writes there.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .events import Event, iso_at, make_event, now_iso

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@dataclass
class RunnerContext:
    """Everything a runner needs for one grant of the tuner."""

    data_dir: Path
    edition: str = "system"
    freq_hz: int | None = None
    simulate: bool = False
    fixtures_dir: Path = FIXTURES_DIR
    watch_dir: Path | None = None
    sat_pipeline: str | None = None
    airband_channels: list | None = None
    airband_live: bool = False
    airband_squelch_snr: float | None = None
    airband_gain: int | None = None
    base_time: datetime | None = None
    _stamp_counter: int = field(default=0, repr=False)

    def stamp(self) -> str:
        """A timestamp for an event being emitted right now.

        In simulate mode stamps walk forward from base_time so a
        replayed fixture lands in order inside the current day.
        """
        if self.simulate and self.base_time is not None:
            from datetime import timedelta

            moment = self.base_time + timedelta(seconds=self._stamp_counter)
            self._stamp_counter += 1
            return iso_at(moment)
        return now_iso()


def _restamp(events: list[Event], ctx: RunnerContext) -> list[Event]:
    """In simulate mode, move events to the present and mark them."""
    if not ctx.simulate:
        return events
    from dataclasses import replace

    out = []
    for event in events:
        payload = dict(event.payload)
        payload["simulated"] = True
        out.append(replace(event, ts=ctx.stamp(), payload=payload))
    return out


class Runner:
    """Base class: one decoder family, one external program."""

    name = "runner"
    binary: str | None = None
    mode = "unknown"
    default_freq_hz = 0
    install_hint = ""
    fixture_name: str | None = None

    def __init__(self, binary: str | None = None):
        if binary is not None:
            self.binary = binary

    # -- honesty about presence --------------------------------------

    def available(self) -> bool:
        if self.binary is None:
            return False
        return shutil.which(self.binary) is not None

    def status(self) -> dict:
        ok = self.available()
        return {
            "runner": self.name,
            "binary": self.binary,
            "mode": self.mode,
            "available": ok,
            "hint": "" if ok else self.install_hint,
        }

    def freq(self, ctx: RunnerContext) -> int:
        return int(ctx.freq_hz or self.default_freq_hz)

    # -- parsing (pure, fixture-tested) --------------------------------

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        return []

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        if self.fixture_name is None:
            return []
        path = ctx.fixtures_dir / self.fixture_name
        if not path.is_file():
            return []
        events: list[Event] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            events.extend(self.parse_line(line, ctx))
        return _restamp(events, ctx)

    # -- running -------------------------------------------------------

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        raise NotImplementedError

    def _spawn(self, ctx: RunnerContext) -> list[subprocess.Popen]:
        argv = self.build_argv(ctx)
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        return [proc]

    @staticmethod
    def _pump(stream, sink: "queue.Queue") -> None:
        try:
            for line in stream:
                sink.put(line)
        except (ValueError, OSError):
            pass
        sink.put(None)

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        """Run for up to `duration` seconds, emitting parsed events.

        emit is called with each Event. Returns a summary dict; never
        raises because the binary is missing.
        """
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            # Simulate replays the fixture and returns at once; it does
            # not sit out the dwell the way a real listening grant does.
            return {"runner": self.name, "ran": True, "simulated": True, "events": count}
        if not self.available():
            return {
                "runner": self.name,
                "ran": False,
                "reason": f"{self.binary} not found on PATH",
                "hint": self.install_hint,
                "events": 0,
            }
        procs = self._spawn(ctx)
        lines: "queue.Queue" = queue.Queue()
        pump = threading.Thread(target=self._pump, args=(procs[0].stdout, lines),
                                daemon=True)
        pump.start()
        deadline = time.monotonic() + duration if duration else None
        count = 0
        try:
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if procs[0].poll() is not None:
                        break
                    continue
                if line is None:
                    break
                for event in self.parse_line(line, ctx):
                    emit(event)
                    count += 1
        finally:
            self._stop_all(procs)
        return {"runner": self.name, "ran": True, "events": count}

    @staticmethod
    def _wait(stop_event: threading.Event, duration: float | None) -> None:
        deadline = time.monotonic() + duration if duration else None
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            stop_event.wait(0.05)

    @staticmethod
    def _stop_all(procs: list[subprocess.Popen]) -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# ----------------------------------------------------------------------
# ADS-B - readsb
# ----------------------------------------------------------------------


class ReadsbRunner(Runner):
    """ADS-B at 1090 MHz via readsb's aircraft.json file output.

    readsb is run with --write-json into a scratch directory and the
    aircraft.json file it rewrites every second is polled. An event is
    emitted when an aircraft is first seen, and again when its
    reported state (position, altitude, track, speed, flight) changes
    - not on every poll.
    """

    name = "readsb"
    binary = "readsb"
    mode = "adsb"
    default_freq_hz = 1_090_000_000
    fixture_name = "aircraft.json"
    install_hint = "install readsb (e.g. build from github.com/wiedehopf/readsb, or a distro package)"

    def __init__(self, binary: str | None = None):
        super().__init__(binary)
        self._state: dict[str, tuple] = {}

    @staticmethod
    def _signature(aircraft: dict) -> tuple:
        return (
            aircraft.get("flight", "").strip(),
            round(aircraft.get("lat", 0.0), 3) if "lat" in aircraft else None,
            round(aircraft.get("lon", 0.0), 3) if "lon" in aircraft else None,
            aircraft.get("alt_baro"),
            aircraft.get("track"),
            aircraft.get("gs"),
            aircraft.get("on_ground"),
        )

    def parse_aircraft_json(self, obj: dict, ctx: RunnerContext) -> list[Event]:
        events: list[Event] = []
        for aircraft in obj.get("aircraft", []):
            hex_code = str(aircraft.get("hex", "")).strip().upper()
            if not hex_code:
                continue
            signature = self._signature(aircraft)
            if self._state.get(hex_code) == signature:
                continue
            self._state[hex_code] = signature
            payload = {
                "hex": hex_code,
                "flight": aircraft.get("flight", "").strip() or None,
                "lat": aircraft.get("lat"),
                "lon": aircraft.get("lon"),
                "alt_baro_ft": aircraft.get("alt_baro"),
                "ground_speed_kt": aircraft.get("gs"),
                "track_deg": aircraft.get("track"),
                "vert_rate_fpm": aircraft.get("baro_rate"),
                "squawk": aircraft.get("squawk"),
                "category": aircraft.get("category"),
                "messages": aircraft.get("messages"),
            }
            events.append(
                make_event(
                    mode="adsb",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload=payload,
                    ts=ctx.stamp(),
                    rssi_dbfs=aircraft.get("rssi"),
                    emitter=hex_code,
                )
            )
        return _restamp(events, ctx) if ctx.simulate else events

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        path = ctx.fixtures_dir / self.fixture_name
        if not path.is_file():
            return []
        self._state = {}
        obj = json.loads(path.read_text(encoding="utf-8"))
        return self.parse_aircraft_json(obj, ctx)

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            # Simulate replays the fixture and returns at once; it does
            # not sit out the dwell the way a real listening grant does.
            return {"runner": self.name, "ran": True, "simulated": True, "events": count}
        if not self.available():
            return {"runner": self.name, "ran": False,
                    "reason": "readsb not found on PATH", "hint": self.install_hint,
                    "events": 0}
        json_dir = Path(ctx.data_dir) / "run" / "readsb"
        json_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            self.binary,
            "--device-type", "rtlsdr",
            "--freq", str(self.freq(ctx)),
            "--quiet",
            "--write-json", str(json_dir),
            "--write-json-every", "1",
        ]
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        json_file = json_dir / "aircraft.json"
        deadline = time.monotonic() + duration if duration else None
        last_mtime = 0.0
        count = 0
        try:
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if proc.poll() is not None:
                    break
                try:
                    mtime = os.path.getmtime(json_file)
                except OSError:
                    mtime = 0.0
                if mtime and mtime != last_mtime:
                    last_mtime = mtime
                    try:
                        obj = json.loads(json_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        obj = None
                    if obj:
                        for event in self.parse_aircraft_json(obj, ctx):
                            emit(event)
                            count += 1
                stop_event.wait(0.5)
        finally:
            self._stop_all([proc])
        return {"runner": self.name, "ran": True, "events": count}


# ----------------------------------------------------------------------
# Sensors - rtl_433
# ----------------------------------------------------------------------


class Rtl433Runner(Runner):
    """Neighbourhood sensors at 433/915 MHz via rtl_433 -F json."""

    name = "rtl_433"
    binary = "rtl_433"
    mode = "sensor"
    default_freq_hz = 433_920_000
    fixture_name = "rtl_433.jsonl"
    install_hint = "apt install rtl-433 (or build from github.com/merbanan/rtl_433)"

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        return [self.binary, "-f", str(self.freq(ctx)), "-F", "json", "-M", "level"]

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        model = str(obj.get("model", "unknown"))
        ident = obj.get("id", obj.get("channel", ""))
        emitter = f"{model}:{ident}" if ident != "" else model
        return [
            make_event(
                mode="sensor",
                decoder=self.name,
                edition=ctx.edition,
                freq_hz=self.freq(ctx),
                payload=obj,
                ts=ctx.stamp(),
                rssi_dbfs=obj.get("rssi"),
                emitter=emitter,
            )
        ]


# ----------------------------------------------------------------------
# VDL2 - dumpvdl2
# ----------------------------------------------------------------------


class DumpVdl2Runner(Runner):
    """VDL Mode 2 datalink via dumpvdl2's JSON output.

    dumpvdl2 prints one pretty-printed JSON object per message, so
    parsing is a stream job (raw_decode over a growing buffer), not a
    line job. parse_objects() is the fixture-tested core.
    """

    name = "dumpvdl2"
    binary = "dumpvdl2"
    mode = "vdl2"
    default_freq_hz = 136_975_000
    fixture_name = "dumpvdl2.json"
    install_hint = "build dumpvdl2 from github.com/szpajder/dumpvdl2 (needs libacars)"

    def __init__(self, binary: str | None = None):
        super().__init__(binary)
        self._buffer = ""

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        return [self.binary, "--rtlsdr", "0", "--gain", "49", str(self.freq(ctx))]

    def parse_objects(self, text: str, ctx: RunnerContext) -> list[Event]:
        buffer = self._buffer + text
        decoder = json.JSONDecoder()
        events: list[Event] = []
        index = 0
        while index < len(buffer):
            while index < len(buffer) and buffer[index] in " \t\r\n":
                index += 1
            if index >= len(buffer):
                break
            try:
                obj, end = decoder.raw_decode(buffer, index)
            except json.JSONDecodeError:
                break
            index = end
            event = self._object_to_event(obj, ctx)
            if event is not None:
                events.append(event)
        self._buffer = buffer[index:]
        return events

    def _object_to_event(self, obj: dict, ctx: RunnerContext) -> Event | None:
        if not isinstance(obj, dict) or "vdl2" not in obj:
            return None
        vdl2 = obj["vdl2"]
        src = (vdl2.get("src") or {})
        addr = src.get("addr")
        emitter = f"{addr:06X}" if isinstance(addr, int) else None
        freq = vdl2.get("freq")
        return make_event(
            mode="vdl2",
            decoder=self.name,
            edition=ctx.edition,
            freq_hz=int(freq) if freq else self.freq(ctx),
            payload=vdl2,
            ts=ctx.stamp(),
            rssi_dbfs=None,
            emitter=emitter,
        )

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        return self.parse_objects(line + "\n", ctx)

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        path = ctx.fixtures_dir / self.fixture_name
        if not path.is_file():
            return []
        self._buffer = ""
        events = self.parse_objects(path.read_text(encoding="utf-8"), ctx)
        return _restamp(events, ctx)


# ----------------------------------------------------------------------
# ACARS - acarsdec
# ----------------------------------------------------------------------

_ACARS_HEADER = re.compile(r"\[#\d+\s+\(F:([\d.]+)\s*Mhz\)")
_ACARS_KV = re.compile(r"^([A-Za-z ]{2,16}):\s*(.*)$")


class AcarsdecRunner(Runner):
    """ACARS via acarsdec. Recent builds print JSON with -o json;
    older ones print text blocks. Both are parsed: JSON lines first,
    and a tolerant block parser as the fallback.
    """

    name = "acarsdec"
    binary = "acarsdec"
    mode = "acars"
    default_freq_hz = 131_550_000
    fixture_name = "acarsdec.jsonl"
    text_fixture_name = "acarsdec.txt"
    install_hint = "build acarsdec from github.com/f00b4r0/acarsdec (or the airframes fork)"

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        mhz = self.freq(ctx) / 1e6
        return [self.binary, "-o", "json", "-r", "0", f"{mhz:.3f}"]

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return []
            payload = {
                "label": obj.get("label"),
                "mode": obj.get("mode"),
                "tail": obj.get("tail") or obj.get("reg"),
                "flight": obj.get("flight"),
                "from": obj.get("from") or obj.get("fromaddr"),
                "to": obj.get("to") or obj.get("toaddr"),
                "text": obj.get("text") or obj.get("msg_text"),
                "freq_mhz": obj.get("freq"),
                "level_db": obj.get("level"),
            }
            emitter = payload["tail"] or payload["flight"]
            rssi = obj.get("level")
            return [
                make_event(
                    mode="acars",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload=payload,
                    ts=ctx.stamp(),
                    rssi_dbfs=rssi if isinstance(rssi, (int, float)) else None,
                    emitter=emitter,
                )
            ]
        return self.parse_text_block(stripped, ctx)

    def parse_text_block(self, text: str, ctx: RunnerContext) -> list[Event]:
        """Parse one classic acarsdec text block (or a whole file)."""
        events: list[Event] = []
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in text.splitlines():
            if _ACARS_HEADER.search(line):
                if current:
                    blocks.append(current)
                current = [line]
            elif current:
                current.append(line)
        if current:
            blocks.append(current)
        for block in blocks:
            payload: dict = {"raw_block": "\n".join(block)}
            freq_hz = self.freq(ctx)
            header = _ACARS_HEADER.search(block[0])
            if header:
                freq_hz = int(float(header.group(1)) * 1e6)
            body_lines = []
            for line in block[1:]:
                match = _ACARS_KV.match(line.strip())
                if match and match.group(1).strip().lower() in (
                    "mode", "label", "id", "nak", "from", "to", "text",
                ):
                    payload[match.group(1).strip().lower()] = match.group(2).strip()
                elif line.strip():
                    body_lines.append(line.strip())
            if body_lines and "text" not in payload:
                payload["text"] = " ".join(body_lines)
            events.append(
                make_event(
                    mode="acars",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=freq_hz,
                    payload=payload,
                    ts=ctx.stamp(),
                )
            )
        return events

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        events = super().fixture_events(ctx)
        text_path = ctx.fixtures_dir / self.text_fixture_name
        if text_path.is_file():
            events.extend(
                _restamp(
                    self.parse_text_block(
                        text_path.read_text(encoding="utf-8", errors="replace"), ctx
                    ),
                    ctx,
                )
            )
        return events


# ----------------------------------------------------------------------
# AIS - AIS-catcher
# ----------------------------------------------------------------------


class AisRunner(Runner):
    """Ships at 161.975/162.025 MHz via AIS-catcher JSON output."""

    name = "ais"
    binary = "AIS-catcher"
    mode = "ais"
    default_freq_hz = 162_000_000
    fixture_name = "ais.jsonl"
    install_hint = "build AIS-catcher from github.com/jvde-github/AIS-catcher"

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        return [self.binary, "-d", "0", "-s", "1536000", "-o", "JSON"]

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        mmsi = obj.get("mmsi")
        if mmsi is None:
            return []
        return [
            make_event(
                mode="ais",
                decoder=self.name,
                edition=ctx.edition,
                freq_hz=self.freq(ctx),
                payload=obj,
                ts=ctx.stamp(),
                emitter=str(mmsi),
            )
        ]


# ----------------------------------------------------------------------
# Pagers + SAME alerts - multimon-ng behind rtl_fm
# ----------------------------------------------------------------------

_POCSAG_RE = re.compile(
    r"POCSAG(\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d+)\s+(Alpha|Numeric):\s*(.*)"
)


class MultimonRunner(Runner):
    """POCSAG pagers and EAS/SAME headers, demodulated by rtl_fm and
    decoded by multimon-ng. Two binaries, one runner: the pipeline is
    the decoder. Both must be present or the runner is unavailable.
    """

    name = "multimon"
    binary = "multimon-ng"
    mode = "pocsag"
    default_freq_hz = 929_612_500
    fixture_name = "multimon.txt"
    install_hint = "apt install multimon-ng rtl-sdr (multimon-ng needs rtl_fm from the rtl-sdr tools)"

    def available(self) -> bool:
        return shutil.which("multimon-ng") is not None and shutil.which("rtl_fm") is not None

    def status(self) -> dict:
        state = super().status()
        state["pipeline"] = "rtl_fm | multimon-ng"
        state["available"] = self.available()
        if not state["available"]:
            state["hint"] = self.install_hint
        return state

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        return [
            "multimon-ng", "-a", "POCSAG512", "-a", "POCSAG1200",
            "-a", "POCSAG2400", "-a", "EAS", "-t", "raw", "-",
        ]

    def _spawn(self, ctx: RunnerContext) -> list[subprocess.Popen]:
        rtl_fm = subprocess.Popen(
            ["rtl_fm", "-f", str(self.freq(ctx)), "-s", "22050", "-g", "40", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        multimon = subprocess.Popen(
            self.build_argv(ctx),
            stdin=rtl_fm.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        rtl_fm.stdout.close()
        return [multimon, rtl_fm]

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        stripped = line.strip()
        match = _POCSAG_RE.search(stripped)
        if match:
            baud, address, function, kind, message = match.groups()
            return [
                make_event(
                    mode="pocsag",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload={
                        "baud": int(baud),
                        "address": address,
                        "function": int(function),
                        "kind": kind.lower(),
                        "message": message.strip(),
                    },
                    ts=ctx.stamp(),
                    emitter=f"pager:{address}",
                )
            ]
        if stripped.startswith("EAS"):
            return [
                make_event(
                    mode="eas",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload={"raw": stripped, "source": "multimon-ng EAS"},
                    ts=ctx.stamp(),
                    emitter="eas:same",
                )
            ]
        return []


# ----------------------------------------------------------------------
# NOAA Weather Radio - presence by audio energy, SAME via multimon-ng
# ----------------------------------------------------------------------


class WxRadioRunner(Runner):
    """NOAA Weather Radio presence on the 162 MHz channels.

    What this runner honestly does: demodulate the channel with
    rtl_fm, measure the energy of the audio, and log a presence event
    while a signal is there - presence inferred from demodulated
    audio energy, not a decode of the audio. When multimon-ng is also
    installed, the same audio is teed to it and any EAS/SAME alert
    headers it decodes become eas events in their own right.
    """

    name = "wxradio"
    binary = "rtl_fm"
    mode = "wxradio"
    default_freq_hz = 162_550_000
    fixture_name = None
    install_hint = "apt install rtl-sdr (rtl_fm); add multimon-ng for SAME/EAS alert decoding"
    energy_threshold = 300.0  # RMS of int16 audio; hiss sits well below voice+tones

    def available(self) -> bool:
        return shutil.which("rtl_fm") is not None

    def status(self) -> dict:
        state = super().status()
        state["same_decoding"] = shutil.which("multimon-ng") is not None
        return state

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        return _restamp(
            [
                make_event(
                    mode="wxradio",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload={
                        "method": "audio-energy",
                        "note": "simulated presence event - no audio was measured",
                        "rms": 812.0,
                    },
                    ts=ctx.stamp(),
                    emitter="wxradio:162.550",
                )
            ],
            ctx,
        )

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            # Simulate replays the fixture and returns at once; it does
            # not sit out the dwell the way a real listening grant does.
            return {"runner": self.name, "ran": True, "simulated": True, "events": count}
        if not self.available():
            return {"runner": self.name, "ran": False,
                    "reason": "rtl_fm not found on PATH", "hint": self.install_hint,
                    "events": 0}
        import array

        rtl_fm = subprocess.Popen(
            ["rtl_fm", "-f", str(self.freq(ctx)), "-s", "200000",
             "-r", "48000", "-g", "40", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        procs = [rtl_fm]
        multimon = None
        lines: "queue.Queue" = queue.Queue()
        if shutil.which("multimon-ng") is not None:
            multimon = subprocess.Popen(
                ["multimon-ng", "-a", "EAS", "-t", "raw", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            procs.append(multimon)
            threading.Thread(target=self._pump, args=(multimon.stdout, lines),
                             daemon=True).start()
        eas_parser = MultimonRunner()
        deadline = time.monotonic() + duration if duration else None
        count = 0
        hot_seconds = 0
        last_presence = 0.0
        try:
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                chunk = rtl_fm.stdout.read(48000 * 2)  # one second of int16 audio
                if not chunk:
                    break
                if multimon is not None:
                    try:
                        multimon.stdin.write(chunk)
                        multimon.stdin.flush()
                    except (BrokenPipeError, OSError):
                        multimon = None
                samples = array.array("h")
                samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
                if samples:
                    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                    if rms >= self.energy_threshold:
                        hot_seconds += 1
                        if hot_seconds >= 3 and time.monotonic() - last_presence >= 300:
                            last_presence = time.monotonic()
                            emit(
                                make_event(
                                    mode="wxradio",
                                    decoder=self.name,
                                    edition=ctx.edition,
                                    freq_hz=self.freq(ctx),
                                    payload={
                                        "method": "audio-energy",
                                        "note": "presence inferred from demodulated audio energy, not a decode",
                                        "rms": round(rms, 1),
                                    },
                                    ts=ctx.stamp(),
                                    emitter=f"wxradio:{self.freq(ctx) / 1e6:.3f}",
                                )
                            )
                            count += 1
                    else:
                        hot_seconds = 0
                while True:
                    try:
                        line = lines.get_nowait()
                    except queue.Empty:
                        break
                    if line is None:
                        break
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    for event in eas_parser.parse_line(line, ctx):
                        emit(event)
                        count += 1
        finally:
            self._stop_all(procs)
        return {"runner": self.name, "ran": True, "events": count}


# ----------------------------------------------------------------------
# Airband voice - RTLSDR-Airband
# ----------------------------------------------------------------------

# The station's channels by default: FAA data as published by AirNav
# (effective 2026-01-22), researched 2026-11-10.
# Override wholesale with "airband_channels" in config.json - a list
# of {"freq_hz": ..., "label": ...} - and set "airband_live": true
# to add a live PulseAudio output on each channel.
DEFAULT_AIRBAND_CHANNELS = [
    {"freq_hz": 120_550_000, "label": "Atlanta Center - Montgomery RCAG"},
    {"freq_hz": 120_675_000, "label": "Montgomery ATIS"},
    {"freq_hz": 120_925_000, "label": "Troy ATIS"},
    {"freq_hz": 121_100_000, "label": "Cairns Approach"},
    {"freq_hz": 121_500_000, "label": "Guard"},
    {"freq_hz": 121_700_000, "label": "Montgomery Ground"},
    {"freq_hz": 121_900_000, "label": "Troy Ground"},
    {"freq_hz": 122_800_000, "label": "CTAF - Troy, Greenville, Enterprise"},
    {"freq_hz": 123_050_000, "label": "Andalusia CTAF"},
    {"freq_hz": 124_000_000, "label": "Montgomery Approach"},
    {"freq_hz": 124_300_000, "label": "Troy Tower"},
    {"freq_hz": 128_550_000, "label": "Jacksonville Center at Cairns"},
    {"freq_hz": 132_450_000, "label": "Atlanta Center - Montgomery south"},
    {"freq_hz": 133_450_000, "label": "Cairns Approach - Goshen sector"},
    {"freq_hz": 134_300_000, "label": "Jacksonville Center - Dothan RCAG"},
]

# One dongle slice: rtl_airband demodulates every channel within
# its tuned bandwidth at once. Channels are grouped by the plan:
# a block's frequency is the center of a slice, and the channels
# within this half-width of it are the ones that block hears.
AIRBAND_HALF_SLICE_HZ = 1_000_000

_CLIP_RE = re.compile(
    r"^([A-Za-z0-9]+)_(\d{8})_(\d{6})(?:_(\d{8})_(\d{6}))?\.mp3$"
)


def _mhz(freq_hz: int) -> str:
    text = f"{freq_hz / 1_000_000:.5f}".rstrip("0")
    return text if "." in text else text + ".0"


def _clip_slug(freq_hz: int) -> str:
    return f"CH{int(freq_hz) // 1000}"


class AirbandRunner(Runner):
    """Pilot and controller voice on the 118-137 MHz airband.

    RTLSDR-Airband demodulates every channel inside one tuned slice
    at once, so a grant is a slice: the block's frequency is the
    slice center, and the runner writes an rtl_airband config for
    the channels that fall inside it (the configured channel list,
    or the station defaults). Each transmission becomes one MP3
    clip - rtl_airband splits on transmission - and one voice
    event names the channel, the label and the clip. The clips
    live under <data_dir>/run/airband/clips and stay until the
    owner clears them; the event store is the index of what was
    heard. Who spoke is what the recording says - a voice event
    never guesses a speaker.

    With "airband_live" set in config, every channel also feeds
    one mixer whose PulseAudio output plays on the host's
    speakers - a single mixed stream, the way a scanner sounds,
    rather than one playback stream per channel (the per-channel
    streams measurably sounded worse than the same audio in the
    recorded clips).

    The squelch is an SNR threshold, written as 5.0 dB unless
    "airband_squelch_snr" in config says otherwise. Measured on a
    weak-signal indoor station: at 3.0 dB the noise floor alone
    holds every channel in a slice open at once, and the recorded
    clips are constant-level noise with no speech dynamics; at
    5.0 dB the squelch stays shut on noise and opens on real
    carriers, and the ATIS broadcasts record as chopped but
    plainly speech-modulated audio. Gain defaults to 35 for the
    same reason: at 45 the front end overloads near strong FM
    broadcast signals and raises the apparent noise floor across
    the whole slice.
    """

    squelch_snr_default = 5.0
    gain_default = 35

    name = "airband"
    binary = "rtl_airband"
    mode = "voice"
    default_freq_hz = 121_200_000
    fixture_name = None
    install_hint = ("build RTLSDR-Airband from "
                    "github.com/charlie-foxtrot/RTLSDR-Airband")

    def status(self) -> dict:
        state = super().status()
        state["channels"] = len(DEFAULT_AIRBAND_CHANNELS)
        return state

    def channels_for(self, ctx: RunnerContext) -> list[dict]:
        """The channels this grant hears: configured list or the
        station defaults, narrowed to the block's slice."""
        center = self.freq(ctx)
        configured = ctx.airband_channels or DEFAULT_AIRBAND_CHANNELS
        inside = [
            ch for ch in configured
            if abs(int(ch["freq_hz"]) - center) <= AIRBAND_HALF_SLICE_HZ
        ]
        if not inside:
            inside = [{"freq_hz": center, "label": "airband"}]
        return inside

    def config_text(self, ctx: RunnerContext, channels: list[dict],
                    clips_dir: Path) -> str:
        gain = (ctx.airband_gain if ctx.airband_gain is not None
                else self.gain_default)
        lines = ["# written by passband for one grant of the tuner"]
        if ctx.airband_live:
            lines += [
                "mixers: {",
                "  live: {",
                "    outputs: (",
                "      {",
                '        type = "pulse";',
                '        stream_name = "passband live";',
                "      }",
                "    );",
                "  }",
                "};",
            ]
        lines += [
            "devices:",
            "({",
            '  type = "rtlsdr";',
            "  index = 0;",
            f"  gain = {gain};",
            f"  centerfreq = {_mhz(self.freq(ctx))};",
            "  channels:",
            "  (",
        ]
        squelch = (ctx.airband_squelch_snr
                   if ctx.airband_squelch_snr is not None
                   else self.squelch_snr_default)
        for channel in channels:
            freq_hz = int(channel["freq_hz"])
            lines += [
                "    {",
                f"      freq = {_mhz(freq_hz)};",
                f"      squelch_snr_threshold = {squelch:.1f};",
                "      outputs: (",
                "        {",
                '          type = "file";',
                f'          directory = "{clips_dir}";',
                f'          filename_template = "{_clip_slug(freq_hz)}";',
                "          split_on_transmission = true;",
                "          split_tx_drop_shorter_than = 0.3;",
                "        }" + ("," if ctx.airband_live else ""),
            ]
            if ctx.airband_live:
                lines += [
                    "        {",
                    '          type = "mixer";',
                    '          name = "live";',
                    "        }",
                ]
            lines += ["      );", "    },"] 
        lines += ["  );", " }", ");", ""]
        return "\n".join(lines)

    @staticmethod
    def parse_clip_name(name: str):
        """(slug, started-datetime-in-UTC) for a transmission clip."""
        match = _CLIP_RE.match(name)
        if not match:
            return None
        date, clock = match.group(2), match.group(3)
        try:
            started = datetime.strptime(date + clock, "%Y%m%d%H%M%S")
        except ValueError:
            return None
        return match.group(1), started

    def _clip_event(self, ctx: RunnerContext, path: Path,
                    channels: list[dict]) -> Event | None:
        parsed = self.parse_clip_name(path.name)
        if parsed is None:
            return None
        slug, started = parsed
        channel = next(
            (ch for ch in channels if _clip_slug(int(ch["freq_hz"])) == slug),
            None,
        )
        freq_hz = int(channel["freq_hz"]) if channel else self.freq(ctx)
        label = channel["label"] if channel else "airband"
        # rtl_airband names its clips in UTC, not local time - the
        # first live runs proved it by landing six hours ahead.
        ts = started.replace(tzinfo=timezone.utc).isoformat()
        return make_event(
            mode="voice",
            decoder=self.name,
            edition=ctx.edition,
            freq_hz=freq_hz,
            payload={
                "label": label,
                "clip": str(path),
                "bytes": path.stat().st_size,
                "started_utc": started.isoformat(),
            },
            ts=ts,
        )

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        channels = self.channels_for(ctx)
        events = []
        for channel in channels[:3]:
            events.append(
                make_event(
                    mode="voice",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=int(channel["freq_hz"]),
                    payload={
                        "label": channel["label"],
                        "clip": None,
                        "note": ("simulated transmission event - "
                                 "no audio was received"),
                    },
                    ts=ctx.stamp(),
                )
            )
        return events

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            return {"runner": self.name, "ran": True, "simulated": True,
                    "events": count}
        if not self.available():
            return {"runner": self.name, "ran": False,
                    "reason": "rtl_airband not found on PATH",
                    "hint": self.install_hint, "events": 0}
        channels = self.channels_for(ctx)
        run_dir = Path(ctx.data_dir) / "run" / "airband"
        clips_dir = run_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        config_path = run_dir / "airband.conf"
        config_path.write_text(
            self.config_text(ctx, channels, clips_dir), encoding="utf-8"
        )
        argv = [self.binary, "-F", "-e", "-c", str(config_path)]
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        started = time.monotonic()
        deadline = started + duration if duration else None
        emitted: set[str] = set()
        count = 0

        def sweep(final: bool) -> None:
            nonlocal count
            for path in sorted(clips_dir.glob("*.mp3")):
                name = path.name
                if name in emitted:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size <= 0:
                    continue
                # A clip still being written has a fresh mtime; a
                # transmission is over when its file has been quiet
                # for a few seconds. The final sweep takes whatever
                # the grant's end left behind.
                if not final and (time.time() - stat.st_mtime) < 3.0:
                    continue
                event = self._clip_event(ctx, path, channels)
                if event is not None:
                    emitted.add(name)
                    emit(event)
                    count += 1

        try:
            while not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if proc.poll() is not None:
                    break
                sweep(final=False)
                stop_event.wait(1.0)
        finally:
            self._stop_all([proc])
        sweep(final=True)
        if count == 0 and time.monotonic() - started < 3.0:
            # rtl_airband died at startup - a bad config or a busy
            # device. Say so; a silent "ran" here once cost the
            # owner a whole evening of listening to nothing.
            return {"runner": self.name, "ran": False,
                    "reason": ("rtl_airband exited at startup - bad "
                               "config or the tuner is busy"),
                    "events": 0}
        return {"runner": self.name, "ran": True, "events": count,
                "channels": [int(ch["freq_hz"]) for ch in channels]}


# ----------------------------------------------------------------------
# Weather satellites - SatDump
# ----------------------------------------------------------------------

_PRODUCT_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".json")


class SatDumpRunner(Runner):
    """Meteor-M LRPT via SatDump, run live for the length of a pass.

    Passband builds the satdump live command line (the pipeline id is a
    plan setting - SatDump renames pipelines between versions, and
    the plan is where that fact belongs), lets it run for the grant,
    then shelves what it produced: every product file that appeared in
    the output directory is named in one satellite event. No products
    is reported as no products, never dressed up.
    """

    name = "satdump"
    binary = "satdump"
    mode = "satellite"
    default_freq_hz = 137_620_000
    fixture_name = "satdump_products.txt"
    install_hint = "install SatDump from github.com/SatDump/SatDump (release packages for Linux)"
    default_pipeline = "metm2-3"

    def build_argv(self, ctx: RunnerContext) -> list[str]:
        out_dir = Path(ctx.data_dir) / "run" / "satdump"
        return [
            self.binary, "live", ctx.sat_pipeline or self.default_pipeline,
            str(out_dir),
            "--samplerate", "2400000",
            "--frequency", str(self.freq(ctx)),
            "--source", "rtlsdr",
        ]

    def _products_since(self, out_dir: Path, since: float) -> list[str]:
        found = []
        if not out_dir.is_dir():
            return found
        for path in sorted(out_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in _PRODUCT_SUFFIXES:
                if path.stat().st_mtime >= since:
                    found.append(str(path.relative_to(out_dir)))
        return found

    def fixture_events(self, ctx: RunnerContext) -> list[Event]:
        path = ctx.fixtures_dir / self.fixture_name
        products = []
        if path.is_file():
            products = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return _restamp(
            [
                make_event(
                    mode="satellite",
                    decoder=self.name,
                    edition=ctx.edition,
                    freq_hz=self.freq(ctx),
                    payload={
                        "satellite": "METEOR-M2 3",
                        "pipeline": ctx.sat_pipeline or self.default_pipeline,
                        "products": products,
                        "product_count": len(products),
                    },
                    ts=ctx.stamp(),
                    emitter="sat:METEOR-M2 3",
                )
            ],
            ctx,
        )

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            # Simulate replays the fixture and returns at once; it does
            # not sit out the dwell the way a real listening grant does.
            return {"runner": self.name, "ran": True, "simulated": True, "events": count}
        if not self.available():
            return {"runner": self.name, "ran": False,
                    "reason": "satdump not found on PATH", "hint": self.install_hint,
                    "events": 0}
        out_dir = Path(ctx.data_dir) / "run" / "satdump"
        out_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        proc = subprocess.Popen(self.build_argv(ctx), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            self._wait(stop_event, duration)
        finally:
            self._stop_all([proc])
        products = self._products_since(out_dir, started)
        satellite = "METEOR-M2 4" if (ctx.sat_pipeline or "").endswith("4") else "METEOR-M2 3"
        emit(
            make_event(
                mode="satellite",
                decoder=self.name,
                edition=ctx.edition,
                freq_hz=self.freq(ctx),
                payload={
                    "satellite": satellite,
                    "pipeline": ctx.sat_pipeline or self.default_pipeline,
                    "products": products,
                    "product_count": len(products),
                    "note": "pass recorded; products listed are the files SatDump produced"
                    if products else "pass recorded; SatDump produced no product files",
                },
                ts=ctx.stamp(),
                emitter=f"sat:{satellite}",
            )
        )
        return {"runner": self.name, "ran": True, "events": 1}


# ----------------------------------------------------------------------
# HF digital modes - a file-watch runner over WSJT-X style logs
# ----------------------------------------------------------------------

_ALLTXT_RE = re.compile(
    r"^(\d{6})_(\d{6})\s+([\d.]+)\s+(Rx|Tx)\s+(FT8|FT4|WSPR|JT65|JT9|MSK144)\s+(.*)$"
)
_ALLTXT_REST = re.compile(r"^(-?\d+)\s+(-?[\d.]+)\s+(\d+)\s+(.*)$")
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,}(/[A-Z0-9]+)?$")


def _callsign_from_message(message: str) -> str | None:
    tokens = message.replace("<", "").replace(">", "").split()
    if not tokens:
        return None
    candidates = tokens
    if tokens[0] == "CQ" and len(tokens) > 1:
        candidates = tokens[1:]
        if candidates and candidates[0] in ("DX", "NA", "EU", "JA"):
            candidates = candidates[1:]
    for token in candidates:
        if _CALLSIGN_RE.match(token) and any(ch.isdigit() for ch in token):
            return token
    return None


class HfRunner(Runner):
    """FT8/WSPR and friends, honestly: a file-watch runner.

    Passband does not decode FT8 itself. WSJT-X (or any tool the owner
    wires up) writes an ALL.TXT-style log; this runner watches a
    directory for those logs and turns new lines into events. It is
    labelled a file-watch runner in every status it reports, because
    that is what it is.
    """

    name = "hf"
    binary = None
    mode = "ft8"
    default_freq_hz = 14_074_000
    fixture_name = "all.txt"
    install_hint = "no binary needed: point the watch directory at a WSJT-X ALL.TXT log"

    def available(self) -> bool:
        return True  # a file watch needs no binary; an empty dir is a fact, not a failure

    def status(self) -> dict:
        return {
            "runner": self.name,
            "binary": None,
            "mode": self.mode,
            "available": True,
            "kind": "file-watch",
            "hint": "watches a directory for ALL.TXT / WSPR spot logs (WSJT-X writes ALL.TXT)",
        }

    def parse_line(self, line: str, ctx: RunnerContext) -> list[Event]:
        match = _ALLTXT_RE.match(line.strip())
        if not match:
            return []
        yymmdd, hhmmss, mhz, direction, mode, rest = match.groups()
        if direction != "Rx":
            return []
        rest_match = _ALLTXT_REST.match(rest.strip())
        if not rest_match:
            return []
        snr, dt, df, message = rest_match.groups()
        year = 2000 + int(yymmdd[:2])
        ts = (
            f"{year}-{yymmdd[2:4]}-{yymmdd[4:6]}"
            f"T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}Z"
        )
        band_hz = int(float(mhz) * 1e6)
        payload = {
            "band_mhz": float(mhz),
            "snr_db": int(snr),
            "dt_sec": float(dt),
            "audio_offset_hz": int(df),
            "message": message.strip(),
            "source": "ALL.TXT file watch",
        }
        return [
            make_event(
                mode=mode.lower(),
                decoder=self.name,
                edition=ctx.edition,
                freq_hz=band_hz + int(df),
                payload=payload,
                ts=ts,
                emitter=_callsign_from_message(message),
            )
        ]

    def run(self, ctx: RunnerContext, emit, stop_event: threading.Event,
            duration: float | None = None) -> dict:
        if ctx.simulate:
            count = 0
            for event in self.fixture_events(ctx):
                emit(event)
                count += 1
            # Simulate replays the fixture and returns at once; it does
            # not sit out the dwell the way a real listening grant does.
            return {"runner": self.name, "ran": True, "simulated": True, "events": count}
        watch = ctx.watch_dir or (Path(ctx.data_dir) / "hf-watch")
        watch.mkdir(parents=True, exist_ok=True)
        offsets: dict[Path, int] = {}
        deadline = time.monotonic() + duration if duration else None
        count = 0
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            for path in sorted(watch.glob("*.txt")) + sorted(watch.glob("*.log")):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                start_at = offsets.get(path, 0)
                if size < start_at:
                    start_at = 0
                if size == start_at:
                    offsets[path] = size
                    continue
                with open(path, encoding="utf-8", errors="replace") as handle:
                    handle.seek(start_at)
                    chunk = handle.read()
                    offsets[path] = handle.tell()
                for line in chunk.splitlines():
                    for event in self.parse_line(line, ctx):
                        emit(event)
                        count += 1
            stop_event.wait(1.0)
        return {"runner": self.name, "ran": True, "kind": "file-watch", "events": count}


# ----------------------------------------------------------------------
# The runner table
# ----------------------------------------------------------------------

RUNNERS: dict[str, Runner] = {
    runner.name: runner
    for runner in (
        ReadsbRunner(),
        Rtl433Runner(),
        DumpVdl2Runner(),
        AcarsdecRunner(),
        AisRunner(),
        MultimonRunner(),
        WxRadioRunner(),
        AirbandRunner(),
        SatDumpRunner(),
        HfRunner(),
    )
}


def get_runner(name: str) -> Runner:
    """A fresh runner instance by name (runners hold parse state)."""
    cls = {
        "readsb": ReadsbRunner,
        "rtl_433": Rtl433Runner,
        "dumpvdl2": DumpVdl2Runner,
        "acarsdec": AcarsdecRunner,
        "ais": AisRunner,
        "multimon": MultimonRunner,
        "wxradio": WxRadioRunner,
        "airband": AirbandRunner,
        "satdump": SatDumpRunner,
        "hf": HfRunner,
    }.get(name)
    if cls is None:
        raise KeyError(f"unknown decoder runner: {name}")
    return cls()


def runner_statuses() -> list[dict]:
    return [runner.status() for runner in RUNNERS.values()]
