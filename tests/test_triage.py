"""Clip triage: verdicts never guess, compost only takes old static."""

import json
import math
import os
import time
import wave
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from passband import triage


# -- features ------------------------------------------------------------


def _samples(kind: str, seconds: float = 2.0, rate: int = 16000) -> array:
    out = array("h")
    total = int(seconds * rate)
    for i in range(total):
        t = i / rate
        if kind == "constant":
            value = 9000 if i % 2 else -9000
        elif kind == "gated":
            # speech-like gating: bursts of tone at a syllabic rate
            gate = 1.0 if (t % 0.25) < 0.15 else 0.02
            value = int(12000 * gate * math.sin(2 * math.pi * 440 * t))
        else:  # silence
            value = 0
        out.append(max(-32768, min(32767, value)))
    return out


def test_features_distinguish_gated_from_constant():
    gated = triage.clip_features(_samples("gated"), 16000)
    constant = triage.clip_features(_samples("constant"), 16000)
    assert gated["duration_s"] == 2.0
    # gated audio has quiet gaps: a wider dynamic range than a
    # constant-level stream, whose frames never rise above their
    # own floor (active fraction zero by construction)
    assert gated["dyn_range_db"] > constant["dyn_range_db"] + 10
    assert gated["active_fraction"] > constant["active_fraction"]


def test_features_of_silence():
    feats = triage.clip_features(_samples("silence", 1.0), 16000)
    assert feats["duration_s"] == 1.0
    assert feats["rms_dbfs"] < -100


# -- verdict logic ----------------------------------------------------------


def test_classify_voice():
    heard = {"text": "cairns approach, skyhawk eight three five",
             "avg_logprob": -0.6, "no_speech_prob": 0.1,
             "vad_filter": False}
    verdict, _reason = triage.classify(heard, "")
    assert verdict == "voice"


def test_classify_static_when_nothing_heard():
    heard = {"text": "", "avg_logprob": None, "no_speech_prob": None,
             "vad_filter": None}
    verdict, reason = triage.classify(heard, "")
    assert verdict == "static"
    assert "no speech" in reason


def test_classify_static_when_model_says_not_speech():
    heard = {"text": "some hallucinated words", "avg_logprob": -0.4,
             "no_speech_prob": 0.99, "vad_filter": False}
    verdict, _ = triage.classify(heard, "")
    assert verdict == "static"


def test_classify_uncertain_is_untriaged_not_a_guess():
    heard = {"text": "thank you", "avg_logprob": -1.9,
             "no_speech_prob": 0.6, "vad_filter": False}
    verdict, reason = triage.classify(heard, "")
    assert verdict == "untriaged"
    assert "uncertain" in reason


def test_classify_short_clip_never_static():
    heard = {"text": "", "avg_logprob": None, "no_speech_prob": None,
             "vad_filter": None}
    verdict, reason = triage.classify(heard, "", duration_s=0.65)
    assert verdict == "untriaged"
    assert "too short" in reason
    heard_not_speech = {"text": "words", "avg_logprob": -0.4,
                        "no_speech_prob": 0.99, "vad_filter": False}
    verdict, _ = triage.classify(heard_not_speech, "", duration_s=0.5)
    assert verdict == "untriaged"
    verdict, _ = triage.classify(heard, "", duration_s=3.0)
    assert verdict == "static"


def test_classify_absent_stack_is_untriaged():
    verdict, reason = triage.classify(None, "faster-whisper is not installed")
    assert verdict == "untriaged"
    assert "not installed" in reason


# -- end to end over a fake data dir -----------------------------------------


class FakeTranscriber:
    model_name = "fake"

    def __init__(self, hearings):
        self._hearings = hearings

    def available(self):
        return True, ""

    def transcribe(self, samples, rate):
        return self._hearings


def _write_wav(path: Path, kind: str = "gated", seconds: float = 1.0) -> None:
    samples = _samples(kind, seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(samples.tobytes())


def _make_clip(data_dir: Path, name: str) -> Path:
    folder = triage.clips_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / name
    clip.write_bytes(b"fake mp3 bytes")
    return clip


VOICE_HEARD = {"text": "atlanta center, commuter 4295, flight level 340",
               "avg_logprob": -0.8, "no_speech_prob": 0.3,
               "vad_filter": True}
NOTHING_HEARD = {"text": "", "avg_logprob": None, "no_speech_prob": None,
                 "vad_filter": None}


def test_triage_records_and_skips(tmp_path, monkeypatch):
    clip = _make_clip(tmp_path, "CH121100_20261111_001807.mp3")

    def fake_decode(clip_path, work_dir):
        out = Path(work_dir) / "clip.wav"
        _write_wav(out)
        return out

    monkeypatch.setattr(triage, "decode_clip", fake_decode)
    monkeypatch.setattr(triage, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    summary = triage.triage_data_dir(
        tmp_path, transcriber=FakeTranscriber(VOICE_HEARD),
        say=lambda line: None)
    assert summary["voice"] == 1 and summary["clips"] == 1
    records = triage.load_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["verdict"] == "voice"
    assert record["whisper"]["transcript"].startswith("atlanta center")
    assert record["started_utc"] == "2026-11-11T00:18:07+00:00"
    assert "features" in record
    # second run: already judged at the same size - skipped, no new line
    summary = triage.triage_data_dir(
        tmp_path, transcriber=FakeTranscriber(VOICE_HEARD),
        say=lambda line: None)
    assert summary["skipped"] == 1 and summary["clips"] == 0
    assert len(triage.load_records(tmp_path)) == 1
    assert clip.is_file()


def test_triage_blocked_without_ffmpeg(tmp_path, monkeypatch):
    _make_clip(tmp_path, "CH121500_20261111_030744.mp3")
    monkeypatch.setattr(triage, "ffmpeg_path", lambda: None)
    summary = triage.triage_data_dir(
        tmp_path, transcriber=FakeTranscriber(VOICE_HEARD),
        say=lambda line: None)
    assert summary.get("blocked") == "ffmpeg not found on PATH"
    assert triage.load_records(tmp_path) == []


class _SelectiveTranscriber(FakeTranscriber):
    """Hears voice in long clips, nothing in short ones - the fake
    decode below gives the voice clip a longer WAV, so the hearing
    can key on duration the way a real one keys on content."""

    def __init__(self):
        super().__init__(NOTHING_HEARD)

    def transcribe(self, samples, rate):
        if len(samples) / rate >= 1.5:
            return VOICE_HEARD
        return NOTHING_HEARD


def test_compost_takes_only_old_static(tmp_path, monkeypatch):
    def fake_decode(clip_path, work_dir):
        out = Path(work_dir) / "clip.wav"
        seconds = 2.0 if "121100" in Path(clip_path).name else 1.0
        _write_wav(out, seconds=seconds)
        return out

    monkeypatch.setattr(triage, "decode_clip", fake_decode)
    monkeypatch.setattr(triage, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    old_static = _make_clip(tmp_path, "CH121500_20261111_030744.mp3")
    old_voice = _make_clip(tmp_path, "CH121100_20261111_001807.mp3")
    fresh_static = _make_clip(tmp_path, "CH121500_20261111_040000.mp3")
    summary = triage.triage_data_dir(
        tmp_path, transcriber=_SelectiveTranscriber(),
        say=lambda line: None)
    assert summary["voice"] == 1 and summary["static"] == 2
    old = time.time() - 10 * 86400
    for clip in (old_static, old_voice):
        os.utime(clip, (old, old))
    actions = triage.compost_static(tmp_path, retention_days=7,
                                    say=lambda line: None)
    assert [a["clip"] for a in actions] == [str(old_static)]
    assert not old_static.exists()
    assert old_voice.is_file() and fresh_static.is_file()
    verdicts = [r["verdict"] for r in triage.load_records(tmp_path)]
    assert "composted" in verdicts
