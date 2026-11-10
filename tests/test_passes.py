"""Pass prediction sanity, on the bundled representative fixture.

The propagator is Kepler + J2 (labelled approximate everywhere); the
tests assert the shape of truth - a real orbit at a sane altitude
producing ordered, plausible passes - not SGP4-grade times.
"""

from datetime import timedelta
from pathlib import Path

from passband.passes import ACCURACY_NOTE, find_passes, look_angles, parse_tles, propagate

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "tle.txt"


def load():
    return parse_tles(FIXTURE.read_text(encoding="utf-8"))


def test_parse_fixture():
    satellites = load()
    assert len(satellites) == 2
    sat = satellites[0]
    assert sat.name == "METEOR-M2 3"
    assert sat.norad == "54273"
    assert abs(sat.ecc - 0.0008) < 1e-9
    assert abs(sat.incl_deg - 98.7) < 1e-9
    assert sat.epoch.year == 2024


def test_propagate_is_sane():
    sat = load()[0]
    lat, lon, alt = propagate(sat, sat.epoch)
    assert -90 <= lat <= 90
    assert -180 <= lon <= 180
    assert 750 < alt < 900  # Meteor flies at roughly 820 km
    lat2, lon2, alt2 = propagate(sat, sat.epoch + timedelta(minutes=50))
    assert 750 < alt2 < 900
    assert (lat, lon) != (lat2, lon2)  # it moved


def test_find_passes_over_a_known_location():
    sat = load()[0]
    passes = find_passes(sat, 45.0, -100.0, sat.epoch, hours=48)
    assert len(passes) >= 1
    for passage in passes:
        assert passage.aos < passage.tca < passage.los
        assert passage.max_el_deg >= 10
        assert (passage.los - passage.aos).total_seconds() < 20 * 60
    aos_list = [passage.aos for passage in passes]
    assert aos_list == sorted(aos_list)


def test_look_angles_overhead_is_high():
    sat = load()[0]
    lat, lon, _ = propagate(sat, sat.epoch)
    _, elevation = look_angles(sat, sat.epoch, lat, lon)
    assert elevation > 85  # standing under the subpoint: near zenith


def test_accuracy_is_labelled():
    assert "approximate" in ACCURACY_NOTE
    assert "not SGP4" in ACCURACY_NOTE
    sat = load()[0]
    passage = find_passes(sat, 45.0, -100.0, sat.epoch, hours=48)[0]
    assert passage.to_dict()["accuracy"] == ACCURACY_NOTE
