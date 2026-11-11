"""Clip triage: which airband clips hold voice, and which are static.

The airband plan records every transmission the squelch lets through -
dozens of clips an hour, most of them squelch bursts with nobody
speaking. Triage reads the clips back and sorts them:

  voice      a transcription pass heard speech in the clip
  static     two transcription passes heard no speech at all
  untriaged  triage could not judge this clip, and says why

The judging is transcription, and the rules are strict about it. The
optional whisper stack (faster-whisper, a local model, no network)
does the hearing. When it is not installed, every clip is untriaged -
triage never guesses a verdict from loudness alone. The acoustic
features measured for every clip (duration, level, dynamics) are
kept as evidence beside the verdict, and a voice verdict keeps its
transcript, but the features never decide the verdict by themselves:
a loud burst and a loud voice look too much alike for that.

Transcription never receives an initial prompt. A prompt describing
aviation speech makes the model hallucinate the prompt's own phrases
over pure static - measured on this station's own clips - so the
model hears the clip exactly as it is, with no suggestions.

Triage is also the clips' retention rule. A clip judged static may
be composted once it is older than the retention age (config
"triage_retention_days", default 7) - and only when the owner runs
triage with --compost. Voice clips are never composted by triage,
and untriaged clips are kept: an unjudged clip is evidence triage
has not read yet, not noise it may throw away.

Everything is recorded in <data_dir>/triage.jsonl: one line per clip
judged, one per clip composted. The daily report reads that file.
The core stays standard-library only; ffmpeg (decoding) and
faster-whisper (hearing) are external tools this stage shells out
to or imports when present, the way decoder runners shell out to
their decoders.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import wave
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import now_iso

# The preprocessing chain the station's clip analysis was proven
# with: band-limit to the voice band, denoise, normalize, 16 kHz.
FFMPEG_FILTER = "highpass=f=250,lowpass=f=3400,afftdn=nf=-25,loudnorm"
SAMPLE_RATE = 16000
FRAME_SECONDS = 0.025

# Verdict gates, set against the station's confirmed clips (real
# Cairns Approach and Atlanta Center transmissions versus squelch
# bursts from the same evenings). A voice verdict wants a segment
# the model is fairly sure is speech; a static verdict wants either
# silence from both passes or a segment the model is sure is not
# speech. Anything between is untriaged, never a coin flip.
VOICE_MAX_NO_SPEECH = 0.75
VOICE_MIN_LOGPROB = -1.5
STATIC_MIN_NO_SPEECH = 0.95
# A clip shorter than this is never judged static: too little audio
# for a trustworthy "nobody spoke" verdict (a real 0.65 s voice clip
# heard as silence in validation), and static is the only verdict
# that can ever lead to deletion.
SHORT_CLIP_SECONDS = 1.0

DEFAULT_MODEL = "small"
DEFAULT_RETENTION_DAYS = 7


def clips_dir(data_dir) -> Path:
    return Path(data_dir) / "run" / "airband" / "clips"


def triage_path(data_dir) -> Path:
    return Path(data_dir) / "triage.jsonl"


# ----------------------------------------------------------------------
# Decoding and acoustic features (standard library only)
# ----------------------------------------------------------------------


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def decode_clip(clip: Path, work_dir: Path) -> Path | None:
    """Decode one clip to a preprocessed 16 kHz WAV. None on failure."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        return None
    out = Path(work_dir) / (Path(clip).stem + ".wav")
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(clip),
         "-af", FFMPEG_FILTER, "-ar", str(SAMPLE_RATE), str(out)],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not out.is_file():
        return None
    return out


def read_wav_samples(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    samples = array("h")
    samples.frombytes(frames)
    return samples, rate


def clip_features(samples: array, rate: int) -> dict:
    """Measured facts about the audio. Evidence, never the verdict."""
    if rate <= 0 or not samples:
        return {"duration_s": 0.0}
    frame = max(1, int(rate * FRAME_SECONDS))
    count = len(samples) // frame
    features: dict = {"duration_s": round(len(samples) / rate, 2)}
    if count == 0:
        return features
    sums = []
    peak = 0
    total = 0
    for index in range(count):
        chunk = samples[index * frame:(index + 1) * frame]
        acc = 0
        for value in chunk:
            acc += value * value
            total += value * value
            if abs(value) > peak:
                peak = abs(value)
        sums.append(math.sqrt(acc / len(chunk)) if chunk else 0.0)
    overall = math.sqrt(total / len(samples))
    db = [20.0 * math.log10(value / 32768.0 + 1e-9) for value in sums]
    ordered = sorted(db)

    def pct(p: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, int(p * len(ordered))))]

    floor = pct(0.10)
    features.update({
        "rms_dbfs": round(20.0 * math.log10(overall / 32768.0 + 1e-12), 1),
        "peak_dbfs": round(20.0 * math.log10((peak + 1e-9) / 32768.0), 1),
        "dyn_range_db": round(pct(0.95) - floor, 1),
        "active_fraction": round(
            sum(1 for value in db if value > floor + 12.0) / len(db), 3),
    })
    return features


# ----------------------------------------------------------------------
# The hearing: optional faster-whisper, never prompted
# ----------------------------------------------------------------------


class WhisperTranscriber:
    """Lazily loaded faster-whisper model. Absence is a fact, reported.

    Transcription runs twice: once with the voice-activity filter,
    and - when that hears nothing - once without it, because the
    filter rejects weak radio speech it cannot frame. No initial
    prompt is ever passed (see the module docstring).
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model_name = model
        self._model = None
        self._error: str | None = None

    def available(self) -> tuple[bool, str]:
        if self._model is not None:
            return True, ""
        if self._error is not None:
            return False, self._error
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self._error = "faster-whisper is not installed"
            return False, self._error
        try:
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8")
        except Exception as exc:  # model missing, no network, bad path
            self._error = f"whisper model {self.model_name!r} unavailable: {exc}"
            return False, self._error
        return True, ""

    def transcribe(self, samples: array, rate: int) -> dict:
        """Best hearing of one clip across the two passes."""
        import numpy as np

        audio = np.frombuffer(samples.tobytes(), dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0
        best: dict | None = None
        for vad in (True, False):
            segments, _info = self._model.transcribe(
                audio, language="en", beam_size=5, vad_filter=vad,
                condition_on_previous_text=False,
            )
            segments = list(segments)
            if not segments:
                continue
            heard = {
                "text": " ".join(s.text.strip() for s in segments).strip(),
                "avg_logprob": max(s.avg_logprob for s in segments),
                "no_speech_prob": min(s.no_speech_prob for s in segments),
                "vad_filter": vad,
            }
            if best is None or heard["avg_logprob"] > best["avg_logprob"]:
                best = heard
            if best["no_speech_prob"] < VOICE_MAX_NO_SPEECH:
                break
        return best or {"text": "", "avg_logprob": None,
                        "no_speech_prob": None, "vad_filter": None}


def classify(heard: dict | None, stack_error: str,
             duration_s: float | None = None) -> tuple[str, str]:
    """(verdict, reason) from one clip's hearing. Never guesses."""
    if heard is None:
        return "untriaged", stack_error or "whisper stack unavailable"
    verdict: str | None = None
    reason = ""
    if not heard["text"]:
        verdict, reason = "static", "no speech heard in two transcription passes"
    else:
        no_speech = heard["no_speech_prob"]
        logprob = heard["avg_logprob"]
        if no_speech is not None and no_speech >= STATIC_MIN_NO_SPEECH:
            verdict, reason = "static", ("transcription produced text the "
                                         "model itself scores as not speech")
        elif (no_speech is not None and logprob is not None
                and no_speech < VOICE_MAX_NO_SPEECH
                and logprob > VOICE_MIN_LOGPROB):
            verdict, reason = "voice", "speech heard and transcribed"
        else:
            return "untriaged", ("whisper uncertain (no_speech_prob=%s, "
                                 "avg_logprob=%s)" % (no_speech, logprob))
    if (verdict == "static" and duration_s is not None
            and duration_s < SHORT_CLIP_SECONDS):
        return "untriaged", ("clip is %.2f s - too short for a "
                             "trustworthy static verdict" % duration_s)
    return verdict, reason


# ----------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------


def load_records(data_dir) -> list[dict]:
    path = triage_path(data_dir)
    records = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _append_record(data_dir, record: dict) -> None:
    path = triage_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _clip_started(clip: Path) -> str | None:
    from .decoders import AirbandRunner

    parsed = AirbandRunner.parse_clip_name(clip.name)
    if parsed is None:
        return None
    _slug, started = parsed
    return started.replace(tzinfo=timezone.utc).isoformat()


def triage_clip(clip: Path, transcriber, work_dir: Path) -> dict:
    """Judge one clip and return its record (not yet filed)."""
    record: dict = {
        "ts": now_iso(),
        "clip": str(clip),
        "bytes": clip.stat().st_size,
        "started_utc": _clip_started(clip),
    }
    wav = decode_clip(clip, work_dir)
    if wav is None:
        record["verdict"] = "untriaged"
        record["reason"] = ("ffmpeg not available or could not decode "
                            "the clip")
        return record
    samples, rate = read_wav_samples(wav)
    record["features"] = clip_features(samples, rate)
    ok, error = transcriber.available()
    if not ok:
        record["verdict"] = "untriaged"
        record["reason"] = error
        return record
    heard = transcriber.transcribe(samples, rate)
    verdict, reason = classify(
        heard, "", duration_s=record["features"].get("duration_s"))
    record["verdict"] = verdict
    record["reason"] = reason
    record["whisper"] = {
        "model": transcriber.model_name,
        "transcript": heard["text"],
        "avg_logprob": heard["avg_logprob"],
        "no_speech_prob": heard["no_speech_prob"],
        "vad_filter": heard["vad_filter"],
    }
    return record


def triage_data_dir(data_dir, config: dict | None = None,
                    limit: int | None = None, force: bool = False,
                    transcriber=None, say=print) -> dict:
    """Judge every unjudged clip in the data dir's airband clips.

    Returns a summary dict. Records already on file for the same
    clip at the same size are kept unless force is set.
    """
    config = config or {}
    folder = clips_dir(data_dir)
    summary = {"voice": 0, "static": 0, "untriaged": 0, "skipped": 0,
               "clips": 0}
    if not folder.is_dir():
        say(f"no clips directory at {folder}")
        return summary
    if ffmpeg_path() is None:
        say("ffmpeg not found on PATH - clips cannot be decoded, so "
            "nothing was triaged and nothing was recorded")
        summary["blocked"] = "ffmpeg not found on PATH"
        return summary
    done = {}
    for record in load_records(data_dir):
        if record.get("verdict") in ("voice", "static", "untriaged"):
            done[record["clip"]] = record.get("bytes")
    if transcriber is None:
        transcriber = WhisperTranscriber(
            str(config.get("triage_model", DEFAULT_MODEL)))
    clips = sorted(folder.glob("*.mp3"))
    with tempfile.TemporaryDirectory(prefix="passband-triage-") as tmp:
        work = Path(tmp)
        for clip in clips:
            if limit is not None and summary["clips"] >= limit:
                break
            if not force and done.get(str(clip)) == clip.stat().st_size:
                summary["skipped"] += 1
                continue
            record = triage_clip(clip, transcriber, work)
            _append_record(data_dir, record)
            summary[record["verdict"]] += 1
            summary["clips"] += 1
            say(f"{record['verdict']:<9} {clip.name} - {record['reason']}")
    return summary


def compost_static(data_dir, retention_days: int,
                   now: datetime | None = None, say=print) -> list[dict]:
    """Delete static-judged clips older than the retention age.

    Only verdict "static" is ever composted; voice and untriaged
    clips are kept at any age. Each deletion is recorded.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    latest: dict[str, dict] = {}
    for record in load_records(data_dir):
        if record.get("verdict") in ("voice", "static", "untriaged"):
            latest[record["clip"]] = record
    actions = []
    for clip_name, record in sorted(latest.items()):
        if record["verdict"] != "static":
            continue
        clip = Path(clip_name)
        if not clip.is_file():
            continue
        mtime = datetime.fromtimestamp(clip.stat().st_mtime,
                                       tz=timezone.utc)
        if mtime >= cutoff:
            continue
        size = clip.stat().st_size
        clip.unlink()
        entry = {"ts": now_iso(), "clip": clip_name, "bytes": size,
                 "verdict": "composted",
                 "reason": (f"static clip older than the "
                            f"{retention_days}-day retention")}
        _append_record(data_dir, entry)
        actions.append(entry)
        say(f"composted {clip.name} ({size} bytes): {entry['reason']}")
    return actions
