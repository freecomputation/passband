"""Satellite pass prediction from aboard orbital elements.

Parses two-line elements (TLEs) from a local file - reference data
that arrives as an edition and is verified at the gate before this
module ever sees it - propagates each satellite, and predicts passes
over the owner's ground location.

Accuracy, stated plainly and repeated everywhere the numbers are
used: the propagator is Kepler's equation plus the secular J2 terms
(RAAN and perigee precession, mean-motion correction) on a spherical
Earth. It is NOT SGP4. Against real SGP4 it will drift, and for
planning a listening window you should expect a few minutes of error
in AOS/LOS times, growing with the age of the elements. That is good
enough to have the tuner waiting at 137 MHz before a Meteor pass; it
is not good enough for pointing anything, and Passband points nothing.

This module computes; it does not fetch. No element set ever arrives
over a wire inside Passband.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MU = 398600.4418          # km^3 / s^2
RE = 6378.137             # km, equatorial radius (spherical-Earth approx)
J2 = 1.08262668e-3

ACCURACY_NOTE = (
    "approximate passes (Kepler + J2 secular terms, not SGP4): "
    "expect a few minutes of error, growing with element age"
)


@dataclass
class Satellite:
    name: str
    norad: str
    epoch: datetime
    incl_deg: float
    raan_deg: float
    ecc: float
    argp_deg: float
    mean_anom_deg: float
    mean_motion_rev_day: float


@dataclass
class Pass:
    satellite: str
    aos: datetime
    los: datetime
    tca: datetime
    max_el_deg: float

    def to_dict(self) -> dict:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return {
            "satellite": self.satellite,
            "aos": self.aos.strftime(fmt),
            "los": self.los.strftime(fmt),
            "tca": self.tca.strftime(fmt),
            "max_el_deg": round(self.max_el_deg, 1),
            "accuracy": ACCURACY_NOTE,
        }


def _tle_epoch(line1: str) -> datetime:
    year = int(line1[18:20])
    day = float(line1[20:32])
    year += 2000 if year < 57 else 1900
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)


def parse_tles(text: str) -> list[Satellite]:
    """Parse TLE text into satellites. Tolerant: skips junk lines."""
    lines = [line.rstrip("\n") for line in text.splitlines()]
    satellites: list[Satellite] = []
    name = "UNKNOWN"
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            line1, line2 = line, lines[i + 1]
            try:
                satellites.append(
                    Satellite(
                        name=name,
                        norad=line1[2:7].strip(),
                        epoch=_tle_epoch(line1),
                        incl_deg=float(line2[8:16]),
                        raan_deg=float(line2[17:25]),
                        ecc=int(line2[26:33]) / 1e7,
                        argp_deg=float(line2[34:42]),
                        mean_anom_deg=float(line2[43:51]),
                        mean_motion_rev_day=float(line2[52:63]),
                    )
                )
            except (ValueError, IndexError):
                pass
            i += 2
            continue
        if not line.startswith(("1 ", "2 ")):
            name = line.strip()
        i += 1
    return satellites


def _gmst(when: datetime) -> float:
    """Greenwich mean sidereal time, radians."""
    jd = when.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    )
    return math.radians(gmst_deg % 360.0)


def propagate(sat: Satellite, when: datetime) -> tuple[float, float, float]:
    """Sub-satellite point at `when`: (lat_deg, lon_deg, alt_km).

    Kepler + J2 secular terms on a spherical Earth. See the module
    docstring for the honest accuracy statement.
    """
    dt = (when - sat.epoch).total_seconds()
    n0 = sat.mean_motion_rev_day * 2 * math.pi / 86400.0
    a = (MU / (n0 * n0)) ** (1 / 3)
    e = sat.ecc
    incl = math.radians(sat.incl_deg)
    p = a * (1 - e * e)
    j2_factor = J2 * (RE / p) ** 2 * n0
    raan = math.radians(sat.raan_deg) - 1.5 * j2_factor * math.cos(incl) * dt
    argp = math.radians(sat.argp_deg) + 0.75 * j2_factor * (5 * math.cos(incl) ** 2 - 1) * dt
    mean_anom = (
        math.radians(sat.mean_anom_deg)
        + (n0 + 0.75 * j2_factor * math.sqrt(1 - e * e) * (3 * math.cos(incl) ** 2 - 1)) * dt
    )
    # Solve Kepler's equation E - e sin(E) = M.
    m_wrapped = (mean_anom + math.pi) % (2 * math.pi) - math.pi
    ecc_anom = m_wrapped if e < 0.8 else math.pi
    for _ in range(12):
        ecc_anom -= (ecc_anom - e * math.sin(ecc_anom) - m_wrapped) / (1 - e * math.cos(ecc_anom))
    true_anom = 2 * math.atan2(
        math.sqrt(1 + e) * math.sin(ecc_anom / 2),
        math.sqrt(1 - e) * math.cos(ecc_anom / 2),
    )
    r = a * (1 - e * math.cos(ecc_anom))
    xp = r * math.cos(true_anom)
    yp = r * math.sin(true_anom)
    cos_o, sin_o = math.cos(raan), math.sin(raan)
    cos_i, sin_i = math.cos(incl), math.sin(incl)
    cos_w, sin_w = math.cos(argp), math.sin(argp)
    x = (cos_o * cos_w - sin_o * sin_w * cos_i) * xp + (-cos_o * sin_w - sin_o * cos_w * cos_i) * yp
    y = (sin_o * cos_w + cos_o * sin_w * cos_i) * xp + (-sin_o * sin_w + cos_o * cos_w * cos_i) * yp
    z = (sin_w * sin_i) * xp + (cos_w * sin_i) * yp
    theta = _gmst(when)
    x_ecef = math.cos(theta) * x + math.sin(theta) * y
    y_ecef = -math.sin(theta) * x + math.cos(theta) * y
    z_ecef = z
    lat = math.degrees(math.atan2(z_ecef, math.hypot(x_ecef, y_ecef)))
    lon = math.degrees(math.atan2(y_ecef, x_ecef))
    alt = math.sqrt(x_ecef**2 + y_ecef**2 + z_ecef**2) - RE
    return lat, lon, alt


def look_angles(sat: Satellite, when: datetime, lat_deg: float, lon_deg: float,
                alt_m: float = 0.0) -> tuple[float, float]:
    """(azimuth_deg, elevation_deg) of the satellite from a ground point."""
    lat, lon, alt_km = propagate(sat, when)
    # Satellite ECEF on a sphere.
    s_lat, s_lon = math.radians(lat), math.radians(lon)
    sr = RE + alt_km
    sx = sr * math.cos(s_lat) * math.cos(s_lon)
    sy = sr * math.cos(s_lat) * math.sin(s_lon)
    sz = sr * math.sin(s_lat)
    o_lat, o_lon = math.radians(lat_deg), math.radians(lon_deg)
    orad = RE + alt_m / 1000.0
    ox = orad * math.cos(o_lat) * math.cos(o_lon)
    oy = orad * math.cos(o_lat) * math.sin(o_lon)
    oz = orad * math.sin(o_lat)
    rx, ry, rz = sx - ox, sy - oy, sz - oz
    # ENU at the observer.
    east = -math.sin(o_lon) * rx + math.cos(o_lon) * ry
    north = (-math.sin(o_lat) * math.cos(o_lon) * rx
             - math.sin(o_lat) * math.sin(o_lon) * ry
             + math.cos(o_lat) * rz)
    up = (math.cos(o_lat) * math.cos(o_lon) * rx
          + math.cos(o_lat) * math.sin(o_lon) * ry
          + math.sin(o_lat) * rz)
    rng = math.sqrt(east**2 + north**2 + up**2)
    elevation = math.degrees(math.asin(up / rng)) if rng else -90.0
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    return azimuth, elevation


def _refine_edge(sat, lat, lon, alt_m, min_el, t_out, t_in) -> datetime:
    """Bisect between an outside sample and an inside sample."""
    for _ in range(14):
        mid = t_out + (t_in - t_out) / 2
        _, el = look_angles(sat, mid, lat, lon, alt_m)
        if el >= min_el:
            t_in = mid
        else:
            t_out = mid
    return t_in


def find_passes(
    sat: Satellite,
    lat_deg: float,
    lon_deg: float,
    start: datetime,
    hours: float = 24.0,
    min_el_deg: float = 10.0,
    alt_m: float = 0.0,
) -> list[Pass]:
    """All passes above min_el_deg in the window, in time order.

    Coarse scan at 20 s, AOS/LOS refined by bisection, TCA and peak
    elevation sampled inside the pass. Accuracy: see ACCURACY_NOTE.
    """
    step = timedelta(seconds=20)
    end = start + timedelta(hours=hours)
    passes: list[Pass] = []
    prev_t = start
    _, prev_el = look_angles(sat, start, lat_deg, lon_deg, alt_m)
    in_pass = prev_el >= min_el_deg
    aos = start if in_pass else None
    t = start + step
    while t <= end:
        _, el = look_angles(sat, t, lat_deg, lon_deg, alt_m)
        inside = el >= min_el_deg
        if inside and not in_pass:
            aos = _refine_edge(sat, lat_deg, lon_deg, alt_m, min_el_deg, prev_t, t)
            in_pass = True
        elif in_pass and not inside:
            los = _refine_edge(sat, lat_deg, lon_deg, alt_m, min_el_deg, t, prev_t)
            assert aos is not None
            span = (los - aos).total_seconds()
            samples = 40
            best_t, best_el = aos, -90.0
            for k in range(samples + 1):
                sample_t = aos + timedelta(seconds=span * k / samples)
                _, sample_el = look_angles(sat, sample_t, lat_deg, lon_deg, alt_m)
                if sample_el > best_el:
                    best_t, best_el = sample_t, sample_el
            passes.append(
                Pass(satellite=sat.name, aos=aos, los=los, tca=best_t, max_el_deg=best_el)
            )
            in_pass = False
            aos = None
        prev_t, prev_el = t, el
        t += step
    return passes
