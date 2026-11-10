"""CLI smoke: the commands a person actually runs must work with no
hardware, no decoder binaries, and no network - exiting 0 and
producing the output they promise."""

import json
from pathlib import Path

from passband import cli


def run_cli(data_dir, *argv):
    return cli.main(["--data-dir", str(data_dir), *argv])


def test_doctor(data_dir, capsys):
    assert run_cli(data_dir, "doctor") == 0
    out = capsys.readouterr().out
    assert "decoders:" in out
    assert "readsb" in out
    assert "not detected" in out or "dongle:" in out


def test_simulate_run_then_read_back(data_dir, capsys):
    assert run_cli(data_dir, "run", "--plan", "weekday", "--once", "--simulate") == 0
    capsys.readouterr()
    assert run_cli(data_dir, "events", "--mode", "adsb") == 0
    out = capsys.readouterr().out
    assert "A1B2C3" in out
    assert run_cli(data_dir, "registry") == 0
    out = capsys.readouterr().out
    assert "A1B2C3" in out
    assert run_cli(data_dir, "report") == 0
    out = capsys.readouterr().out
    assert "Passband daily summary" in out
    assert "Events heard: 3" in out


def test_capture_simulate(data_dir, capsys):
    assert run_cli(
        data_dir, "capture", "--freq", "137620000", "--seconds", "0.01",
        "--simulate",
    ) == 0
    out = capsys.readouterr().out
    assert "sigmf-data" in out
    assert list((data_dir / "snippets").glob("*.sigmf-data"))


def test_plan_list_and_show(data_dir, capsys):
    assert run_cli(data_dir, "plan", "list") == 0
    out = capsys.readouterr().out
    for name in ("weekday", "satellite", "hf-night"):
        assert name in out
    assert run_cli(data_dir, "plan", "show", "weekday") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["name"] == "weekday"


def test_edition_roundtrip_via_cli(data_dir, tmp_path, capsys):
    src = tmp_path / "elements"
    src.mkdir()
    (src / "tle.txt").write_text("elements\n", encoding="utf-8")
    assert run_cli(
        data_dir, "edition", "shelve", "--src", str(src), "--name", "tle",
        "--kind", "reference", "--version", "1", "--source", "test",
        "--need", "testing",
    ) == 0
    capsys.readouterr()
    assert run_cli(data_dir, "edition", "verify", "tle@1") == 0
    assert run_cli(data_dir, "edition", "list") == 0
    assert "tle@1" in capsys.readouterr().out


def test_passes_without_location_is_a_clean_refusal(data_dir):
    assert run_cli(data_dir, "passes") == 2


def test_passes_with_config_and_elements(data_dir, capsys):
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "tle.txt"
    (data_dir).mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        json.dumps({"lat": 45.0, "lon": -100.0, "tle_file": str(fixture)})
    )
    assert run_cli(data_dir, "passes", "--hours", "96") == 0
    out = capsys.readouterr().out
    assert "approximate" in out
    assert "METEOR-M2" in out


def test_listen_simulate_rtl433(data_dir, capsys):
    assert run_cli(
        data_dir, "listen", "rtl_433", "--seconds", "1", "--simulate"
    ) == 0
    out = capsys.readouterr().out
    assert "Acurite-5n1" in out


def test_report_html(data_dir, tmp_path, capsys):
    assert run_cli(data_dir, "run", "--plan", "weekday", "--once", "--simulate") == 0
    capsys.readouterr()
    html_path = tmp_path / "report.html"
    assert run_cli(data_dir, "report", "--html", str(html_path)) == 0
    capsys.readouterr()
    text = html_path.read_text()
    assert text.startswith("<!DOCTYPE html>")
    assert "http://" not in text and "https://" not in text  # no external assets
