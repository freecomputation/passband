"""The tuner seam: the one place Passband talks to hardware.

Everything radio-shaped enters through here - detecting the dongle,
checking the driver, and pulling raw samples off it with rtl_sdr.
Nothing else in Passband touches USB or shells out to the rtl-sdr tools
for capture.

The hardware is one RTL-SDR: an RTL2832U with either the R820T family
tuner (Blog V3) or the R828D (Blog V4, with its HF upconverter and
triplexer). The V4 only behaves correctly with the RTL-SDR Blog
drivers; stock librtlsdr builds do not switch its triplexer. This
module reports what it can actually detect and says "unconfirmed" or
"unverified" where it cannot - it never invents a model or a driver.

A SimulatedTuner stands in the same place when no hardware is wanted:
it reports itself as simulated in everything it produces and hands
back deterministic pseudo-samples, so the whole stack - scheduler,
store, reports - can be run and seen working before anything is
plugged in. It decodes nothing; decoder runners replay their recorded
fixtures when the tuner is simulated.
"""

from __future__ import annotations

import ctypes.util
import shutil
import subprocess
from pathlib import Path

SAMPLE_RATE_DEFAULT = 2_400_000


def _run_capture_text(argv: list[str], timeout: float) -> str:
    """Run a tool briefly and return whatever it printed (both streams)."""
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
    except OSError:
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""
    return out.decode("utf-8", errors="replace")


def detect_dongle() -> dict:
    """Ask rtl_test what is plugged in. Never guesses.

    Returns a dict: found (bool), and where known: device string,
    serial, tuner chip, and model - "RTL-SDR Blog V4", "RTL-SDR Blog
    V3 (R820T family)", or "RTL2832U family, model unconfirmed".
    """
    rtl_test = shutil.which("rtl_test")
    if rtl_test is None:
        return {
            "found": False,
            "reason": "rtl_test not found on PATH - the RTL-SDR tools are not installed",
        }
    text = _run_capture_text([rtl_test], timeout=4)
    if "no supported devices found" in text.lower() or "found 0 device" in text.lower():
        return {"found": False, "reason": "rtl_test reports no supported devices"}
    device_line = None
    serial = None
    tuner = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("0:") and "," in stripped:
            device_line = stripped[2:].strip()
            parts = [part.strip() for part in device_line.split(",")]
            for part in parts:
                if part.startswith("SN:"):
                    serial = part[3:].strip()
        if "tuner" in stripped.lower() and "found" in stripped.lower():
            tuner = stripped
    if device_line is None and "found 1 device" not in text.lower():
        return {
            "found": False,
            "reason": "rtl_test ran but no device line was parsed from its output",
            "raw": text.strip()[:400],
        }
    haystack = (text + " " + (tuner or "")).lower()
    if "r828d" in haystack or "blog v4" in haystack:
        model = "RTL-SDR Blog V4"
    elif "r820t" in haystack or "r860" in haystack:
        model = "RTL-SDR Blog V3 (R820T family)"
    else:
        model = "RTL2832U family, model unconfirmed"
    return {
        "found": True,
        "device": device_line,
        "serial": serial,
        "tuner_line": tuner,
        "model": model,
    }


def driver_status() -> dict:
    """Report what can be told about the librtlsdr in use.

    The Blog V4 needs the RTL-SDR Blog fork of librtlsdr to switch its
    upconverter and triplexer. Fork identity is genuinely hard to prove
    from outside, so this reports evidence and lands on "unverified"
    whenever the evidence is not there - which is the honest answer on
    most stock installs.
    """
    library = ctypes.util.find_library("rtlsdr")
    if library is None:
        return {
            "librtlsdr": None,
            "blog_fork": "unverified",
            "note": "librtlsdr not found via the system library path",
        }
    blog_evidence = None
    for base in ("/usr/lib", "/usr/local/lib", "/lib"):
        candidate = Path(base)
        if not candidate.is_dir():
            continue
        for path in candidate.rglob(library):
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if b"rtlsdrblog" in blob.lower() or b"blog v4" in blob.lower():
                blog_evidence = "RTL-SDR Blog fork (markers found in the library)"
            elif b"r828d" in blob.lower() and blog_evidence is None:
                blog_evidence = "library knows the R828D tuner; fork unverified"
            break
        if blog_evidence and "Blog fork" in blog_evidence:
            break
    return {
        "librtlsdr": library,
        "blog_fork": blog_evidence or "unverified",
        "note": "the V4 needs the RTL-SDR Blog drivers for correct upconverter/triplexer switching",
    }


class RtlSdrTuner:
    """The real seam: raw unsigned 8-bit I/Q via rtl_sdr."""

    simulated = False

    def __init__(self):
        self.binary = shutil.which("rtl_sdr")

    def available(self) -> bool:
        return self.binary is not None

    def describe(self) -> dict:
        info = detect_dongle()
        info["driver"] = driver_status()
        info["simulated"] = False
        return info

    def capture_bytes(
        self,
        freq_hz: int,
        seconds: float,
        sample_rate: int = SAMPLE_RATE_DEFAULT,
        gain: float | None = None,
    ) -> bytes:
        """Capture raw cu8 I/Q for `seconds` at `freq_hz`. Receive only."""
        if self.binary is None:
            raise RuntimeError("rtl_sdr not found on PATH")
        n_samples = int(sample_rate * seconds)
        argv = [
            self.binary,
            "-f", str(int(freq_hz)),
            "-s", str(int(sample_rate)),
            "-n", str(n_samples),
        ]
        if gain is not None:
            argv += ["-g", str(gain)]
        argv += ["-"]
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=seconds + 30)
        if proc.returncode != 0:
            raise RuntimeError(
                "rtl_sdr failed: " + proc.stderr.decode("utf-8", errors="replace")[:300]
            )
        return proc.stdout


class SimulatedTuner:
    """A stand-in tuner for demos and tests. Decodes nothing itself.

    capture_bytes returns deterministic pseudo-samples (an LCG stream)
    of exactly the right length, so SigMF writing and retention logic
    run identically to the real path. Everything it reports calls
    itself simulated.
    """

    simulated = True

    def available(self) -> bool:
        return True

    def describe(self) -> dict:
        return {
            "found": True,
            "simulated": True,
            "model": "simulated RTL2832U (no hardware)",
            "driver": {"blog_fork": "n/a - simulated"},
        }

    def capture_bytes(
        self,
        freq_hz: int,
        seconds: float,
        sample_rate: int = SAMPLE_RATE_DEFAULT,
        gain: float | None = None,
    ) -> bytes:
        n_bytes = int(sample_rate * seconds) * 2
        out = bytearray(n_bytes)
        state = (int(freq_hz) & 0xFFFFFFFF) or 1
        for i in range(n_bytes):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            out[i] = (state >> 8) & 0xFF
        return bytes(out)
