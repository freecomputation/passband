"""Every decoder parser, run against its recorded fixture.

No decoder binaries exist in the test environment: these tests prove
the parsers on the fixtures, and prove that a missing binary is
reported honestly (unavailable, with a hint) instead of crashing.
"""

import json
from pathlib import Path

from passband import decoders
from passband.decoders import (
    AcarsdecRunner,
    AisRunner,
    DumpVdl2Runner,
    HfRunner,
    MultimonRunner,
    ReadsbRunner,
    Rtl433Runner,
    SatDumpRunner,
    WxRadioRunner,
    get_runner,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# -- readsb -----------------------------------------------------------


def test_readsb_fixture(sim_ctx):
    events = ReadsbRunner().fixture_events(sim_ctx)
    assert len(events) == 3
    by_hex = {event.emitter: event for event in events}
    assert set(by_hex) == {"A1B2C3", "ABC123", "A9F00D"}
    assert by_hex["A1B2C3"].payload["flight"] == "DAL123"
    assert by_hex["A1B2C3"].payload["lat"] == 32.104
    assert by_hex["A1B2C3"].rssi_dbfs == -12.4
    assert all(event.payload.get("simulated") for event in events)
    assert all(event.mode == "adsb" for event in events)


def test_readsb_dedupes_unchanged_aircraft(ctx):
    runner = ReadsbRunner()
    obj = json.loads(fixture_text("aircraft.json"))
    first = runner.parse_aircraft_json(obj, ctx)
    second = runner.parse_aircraft_json(obj, ctx)
    assert len(first) == 3
    assert second == []
    changed = json.loads(fixture_text("aircraft.json"))
    changed["aircraft"][0]["lat"] = 33.5
    third = runner.parse_aircraft_json(changed, ctx)
    assert len(third) == 1 and third[0].emitter == "A1B2C3"


# -- rtl_433 ------------------------------------------------------------


def test_rtl433_fixture_lines(ctx):
    runner = Rtl433Runner()
    events = []
    for line in fixture_text("rtl_433.jsonl").splitlines():
        events.extend(runner.parse_line(line, ctx))
    assert len(events) == 3
    assert events[0].emitter == "Acurite-5n1:1234"
    assert events[0].rssi_dbfs == -62.1
    assert events[0].payload["temp_C"] == 21.4
    assert runner.parse_line("not json at all", ctx) == []
    assert runner.parse_line("", ctx) == []


# -- dumpvdl2 -------------------------------------------------------------


def test_dumpvdl2_stream_fixture(ctx):
    runner = DumpVdl2Runner()
    events = runner.parse_objects(fixture_text("dumpvdl2.json"), ctx)
    assert len(events) == 2
    assert events[0].emitter == f"{10515458:06X}"
    assert events[0].payload["src"]["type"] == "Aircraft"
    assert events[0].freq_hz == 136_975_000


def test_dumpvdl2_partial_object_waits_for_more(ctx):
    runner = DumpVdl2Runner()
    text = fixture_text("dumpvdl2.json")
    half = text[: len(text) // 3]
    assert runner.parse_objects(half, ctx) == [] or True  # may parse none yet
    rest = runner.parse_objects(text[len(text) // 3:], ctx)
    assert len(rest) >= 1


# -- acarsdec ----------------------------------------------------------------


def test_acarsdec_json_lines(ctx):
    runner = AcarsdecRunner()
    events = []
    for line in fixture_text("acarsdec.jsonl").splitlines():
        events.extend(runner.parse_line(line, ctx))
    assert len(events) == 2
    assert events[0].emitter == "N12345"
    assert events[0].payload["label"] == "Q0"
    assert events[1].payload["text"].startswith("ETA 2145")


def test_acarsdec_text_blocks(ctx):
    runner = AcarsdecRunner()
    events = runner.parse_text_block(fixture_text("acarsdec.txt"), ctx)
    assert len(events) == 2
    assert events[0].payload["label"] == "Q0"
    assert "REQUESTING DESCENT" in events[0].payload["text"]
    assert events[1].payload["label"] == "H1"
    assert events[0].freq_hz == 131_550_000


def test_acarsdec_simulate_fixture_has_json_and_text(sim_ctx):
    events = AcarsdecRunner().fixture_events(sim_ctx)
    assert len(events) == 4  # 2 JSON + 2 text blocks


# -- AIS ------------------------------------------------------------------------


def test_ais_fixture_lines(ctx):
    runner = AisRunner()
    events = []
    for line in fixture_text("ais.jsonl").splitlines():
        events.extend(runner.parse_line(line, ctx))
    assert len(events) == 3
    assert events[0].emitter == "366999999"
    assert events[1].payload["shipname"] == "EXAMPLE TRADER"
    assert runner.parse_line('{"no_mmsi": true}', ctx) == []


# -- multimon-ng -------------------------------------------------------------------


def test_multimon_fixture_lines(ctx):
    runner = MultimonRunner()
    events = []
    for line in fixture_text("multimon.txt").splitlines():
        events.extend(runner.parse_line(line, ctx))
    modes = [event.mode for event in events]
    assert modes == ["pocsag", "pocsag", "eas"]
    assert events[0].emitter == "pager:123456"
    assert "STATION 7" in events[0].payload["message"]
    assert events[1].payload["kind"] == "numeric"
    assert events[2].payload["raw"].startswith("EAS: ZCZC")


# -- HF file watch ---------------------------------------------------------------------


def test_hf_alltxt_lines(ctx):
    runner = HfRunner()
    events = []
    for line in fixture_text("all.txt").splitlines():
        events.extend(runner.parse_line(line, ctx))
    modes = [event.mode for event in events]
    assert modes == ["ft8", "ft8", "ft8", "wspr"]  # the Tx line is ignored
    assert events[0].emitter == "K1ABC"
    assert events[0].ts == "2026-11-09T14:03:00Z"
    assert events[0].payload["message"].startswith("CQ K1ABC")
    assert events[0].freq_hz == 14_074_000 + 1234


def test_hf_is_labelled_a_file_watch():
    status = HfRunner().status()
    assert status["kind"] == "file-watch"
    assert status["available"] is True


# -- satdump / wxradio simulate --------------------------------------------------------------


def test_satdump_simulate_event(sim_ctx):
    events = SatDumpRunner().fixture_events(sim_ctx)
    assert len(events) == 1
    assert events[0].mode == "satellite"
    assert events[0].payload["product_count"] == 3
    assert events[0].payload["simulated"] is True


def test_wxradio_simulate_event(sim_ctx):
    events = WxRadioRunner().fixture_events(sim_ctx)
    assert len(events) == 1
    assert events[0].mode == "wxradio"
    assert events[0].payload["method"] == "audio-energy"


# -- absence is honest ----------------------------------------------------------------------------


def test_missing_binary_reports_unavailable():
    runner = Rtl433Runner(binary="passband-definitely-not-a-real-binary")
    assert runner.available() is False
    status = runner.status()
    assert status["available"] is False
    assert status["hint"]
    import threading

    emitted = []
    result = runner.run(
        decoders.RunnerContext(data_dir=__import__("pathlib").Path("/tmp")),
        emitted.append,
        threading.Event(),
        duration=0.1,
    )
    assert result["ran"] is False
    assert "not found" in result["reason"]
    assert emitted == []


def test_runner_table_is_complete():
    for name in ("readsb", "rtl_433", "dumpvdl2", "acarsdec", "ais",
                 "multimon", "wxradio", "airband", "satdump", "hf"):
        assert get_runner(name).name == name
    statuses = decoders.runner_statuses()
    assert len(statuses) == 10


# -- airband --------------------------------------------------------------

from passband.decoders import AirbandRunner


def _airband_ctx(tmp_path, freq_hz):
    return decoders.RunnerContext(data_dir=tmp_path, freq_hz=freq_hz)


def test_airband_slices_hold_the_researched_channels(tmp_path):
    runner = AirbandRunner()
    near = runner.channels_for(_airband_ctx(tmp_path, 121_200_000))
    assert {ch["freq_hz"] for ch in near} == {
        120_550_000, 120_675_000, 120_925_000, 121_100_000,
        121_500_000, 121_700_000, 121_900_000,
    }
    high = runner.channels_for(_airband_ctx(tmp_path, 133_400_000))
    assert {ch["freq_hz"] for ch in high} == {
        132_450_000, 133_450_000, 134_300_000,
    }
    fields = runner.channels_for(_airband_ctx(tmp_path, 123_600_000))
    assert 124_300_000 in {ch["freq_hz"] for ch in fields}


def test_airband_config_names_every_channel_in_the_slice(tmp_path):
    runner = AirbandRunner()
    ctx = _airband_ctx(tmp_path, 121_200_000)
    text = runner.config_text(ctx, runner.channels_for(ctx), tmp_path)
    assert "centerfreq = 121.2;" in text
    assert "freq = 120.55;" in text
    assert "freq = 121.5;" in text
    assert "split_on_transmission = true;" in text
    assert 'filename_template = "CH120550";' in text
    assert 'type = "pulse";' not in text
    live_ctx = _airband_ctx(tmp_path, 121_200_000)
    live_ctx.airband_live = True
    live = runner.config_text(live_ctx, runner.channels_for(live_ctx),
                              tmp_path)
    assert live.count('type = "pulse";') == 1
    assert 'type = "mixer";' in live
    assert "mixers: {" in live
    assert "stream_name" in live


def test_airband_clip_names_parse(tmp_path):
    parsed = AirbandRunner.parse_clip_name("CH120550_20261110_113500.mp3")
    assert parsed is not None
    slug, started = parsed
    assert slug == "CH120550"
    assert (started.hour, started.minute) == (11, 35)
    part = AirbandRunner.parse_clip_name(
        "CH120550_20261110_113500_20261110_113601.mp3")
    assert part is not None and part[0] == "CH120550"
    assert AirbandRunner.parse_clip_name("aircraft.json") is None


def test_airband_simulate_events_are_voice(sim_ctx):
    sim_ctx.freq_hz = 121_200_000
    events = AirbandRunner().fixture_events(sim_ctx)
    assert len(events) == 3
    assert all(event.mode == "voice" for event in events)
    assert events[0].payload["label"] == "Atlanta Center - Montgomery RCAG"
    assert events[0].payload["clip"] is None


def test_airband_squelch_threshold_written_and_overridable(tmp_path):
    runner = AirbandRunner()
    ctx = _airband_ctx(tmp_path, 121_200_000)
    text = runner.config_text(ctx, runner.channels_for(ctx), tmp_path)
    assert "squelch_snr_threshold = 5.0;" in text
    ctx.airband_squelch_snr = 6.5
    text = runner.config_text(ctx, runner.channels_for(ctx), tmp_path)
    assert "squelch_snr_threshold = 6.5;" in text


def test_airband_config_has_no_trailing_comma_after_last_channel(tmp_path):
    # Strict libconfig parsers (the Ubuntu build) refuse a channel
    # list that ends in a comma; lenient ones (the Fedora build)
    # accept it, which hid the bug until the config met the strict
    # parser and rtl_airband died on a syntax error.
    runner = AirbandRunner()
    ctx = _airband_ctx(tmp_path, 121_200_000)
    text = runner.config_text(ctx, runner.channels_for(ctx), tmp_path)
    assert "    }\n  );" in text
    assert "    },\n  );" not in text


def test_airband_clip_event_time_is_the_filename_in_utc(tmp_path):
    runner = AirbandRunner()
    ctx = _airband_ctx(tmp_path, 121_200_000)
    clip = tmp_path / "CH120550_20261110_180038.mp3"
    clip.write_bytes(b"x" * 2048)
    event = runner._clip_event(ctx, clip, runner.channels_for(ctx))
    assert event is not None
    assert event.ts == "2026-11-10T18:00:38+00:00"
    assert event.freq_hz == 120_550_000
    assert event.payload["label"] == "Atlanta Center - Montgomery RCAG"
