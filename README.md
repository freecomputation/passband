# Passband

*by Free Computation. License: AGPL-3.0.*

Passband is the ear of Fixity. Coffer carries the world's knowledge aboard so a
person can read with no connection; Passband does the same for the radio
spectrum around a person. It listens with one cheap receiver, decodes
what it hears into events, and keeps a local, queryable record of the
invisible traffic of a place - aircraft overhead, weather satellites
passing, ships, sensors, pagers, the weather radio.

The name is the radio word for what a receiver does: a passband
is the slice of spectrum a receiver admits and keeps, and this
system is receive-only - it admits a slice of the air and keeps
what it hears, as events. The design contract this code answers
to is [DESIGN.md](DESIGN.md).

## The laws, in short

- **Sustainment.** Everything works at zero connectivity, indefinitely.
  A connection, when one exists, is only a supply line for editions.
- **Receive-only by construction.** There is no transmit code path
  anywhere in Passband, and no network client code either: the only
  things Passband reads are the tuner (through the rtl-sdr tools) and
  local files.
- **No accounts, no telemetry.** What your Passband hears stays on your
  machine.
- **Editions, not updates.** Decoders and reference data are shelved
  as sealed editions (a manifest of sha256 hashes plus a receipt) and
  verified at the gate before they run. Nothing is ever pushed.
- **The core stays small.** Tuner seam, scheduler, event store,
  editions gate - readable end to end. Decoders are cargo, not core.

## Hardware

One RTL-SDR and a computer you already own. Budget: $0.

- Reference rig: **RTL-SDR Blog V4** (RTL2832U + R828D, 500 kHz to
  1.766 GHz, HF by built-in upconverter, 1 ppm TCXO). End-of-line
  since May 2026.
- The design targets the **RTL2832U family**: a Blog V3 (still in
  production) works too, with HF by direct sampling - weaker below
  14 MHz, and its telescopic dipole wants re-lengthening per band.
- Hard limits, by design: 8-bit samples, about 2.4 MHz of spectrum at
  a time, and **one tuner in one place at a time**. The scheduler
  exists because of that last one.

## Setup

Python 3.10+, standard library only. There is nothing to pip install
for Passband itself.

**Drivers first.** The Blog V4 only behaves correctly with the
RTL-SDR Blog drivers - stock librtlsdr builds do not switch its
upconverter and triplexer. Install the Blog fork of rtl-sdr
(github.com/rtlsdrblog/rtl-sdr-blog), blacklist the dvb_usb_rtl28xxu
kernel module per the Blog instructions, then plug in the dongle.

Decoders are the free programs Passband supervises. Install the ones you
want; Passband reports the rest as unavailable, with hints, and carries
on:

```sh
sudo apt install rtl-sdr rtl-433 multimon-ng      # tools, sensors, pagers
# readsb:     build from github.com/wiedehopf/readsb (ADS-B)
# rtl_airband: build from github.com/charlie-foxtrot/RTLSDR-Airband (airband voice)
# dumpvdl2:   build from github.com/szpajder/dumpvdl2 (VDL2, needs libacars)
# acarsdec:   build from github.com/f00b4r0/acarsdec (ACARS)
# AIS-catcher: build from github.com/jvde-github/AIS-catcher (ships)
# SatDump:    release packages at github.com/SatDump/SatDump (weather satellites)
# WSJT-X:     your distro's wsjtx package (HF - Passband watches its ALL.TXT)
```

## Quickstart

```sh
passband doctor                 # what is plugged in, what is installed
passband run --plan weekday --once --simulate
                             # the whole stack on recorded fixtures -
                             # no hardware, no decoders needed
passband events --mode adsb     # what the simulation heard
passband report                 # today's summary
```

For real listening, set your ground location once (passes need it):

```sh
mkdir -p ~/.local/share/passband
cat > ~/.local/share/passband/config.json <<'EOF'
{"lat": 45.0, "lon": -100.0, "alt_m": 500}
EOF
passband run --plan weekday     # Ctrl-C stops; nothing is left running
passband listen rtl_433 --seconds 300
passband capture --freq 137620000 --seconds 10   # a SigMF snippet, as evidence
```

## How it works

- **Scheduler** (`passband/scheduler.py`) - day plans in `plans/` are
  JSON: blocks naming a decoder, a frequency, a dwell, a priority, a
  window of the day, and what goes dark while the block runs. Every
  grant is logged to `grants.jsonl`. A predicted Meteor pass preempts
  the plan and takes the tuner to SatDump for the pass.
- **Decoders** (`passband/decoders.py`) - one runner per family: readsb
  (ADS-B, via its aircraft.json file), rtl_433, dumpvdl2, acarsdec
  (JSON, with a tolerant parser for its classic text output),
  AIS-catcher, multimon-ng (POCSAG + EAS/SAME behind rtl_fm), weather
  radio (presence from demodulated audio energy; SAME headers decoded
  through multimon-ng when present), airband voice (RTLSDR-Airband
  demodulates every channel inside one tuned slice at once; each
  transmission is kept as an MP3 clip under `run/airband/clips` and
  logged as a voice event naming its channel), SatDump (Meteor-M
  LRPT products), and HF (an honest file-watch over WSJT-X ALL.TXT
  logs - Passband does not decode FT8 itself).
- **Event store** (`passband/store.py`) - the product. Events are one
  JSON object per line in `events/YYYY-MM-DD.jsonl`, append-only.
  A sqlite index is grown from the JSONL for fast queries and is
  disposable: delete it and it is rebuilt.
- **Registry** (`passband/registry.py`) - every emitter heard, first and
  last heard, its usual hours, and the expectation judgement behind
  the report's "unusually absent" list: heard on at least 4 of the
  prior 7 days, silent today.
- **Editions** (`passband/editions.py`) - `passband edition shelve --src DIR
  --name tle --kind reference --version 2026-11 --source ... --need ...`
  seals a directory with a sha256 manifest and writes a receipt
  (the need, what it was made from, the hash it answers to).
  `passband edition verify tle` rehashes. A decoder edition that fails
  verification is refused by the scheduler, and the refusal is logged.
  Orbital elements, aircraft databases, and plans travel as editions.
- **Passes** (`passband/passes.py`) - TLE parsing and prediction from
  aboard elements only. Shelve your elements as the `tle` reference
  edition, set `tle_file` in config.json, or drop `tle.txt` in the
  data directory.
- **Snippets** (`passband/snippets.py`) - triggered raw captures as
  SigMF pairs (datatype `cu8_le`, exactly what rtl_sdr emits),
  composted after **7 days** or beyond a **2 GiB** cap, oldest first,
  every compost logged. Both bounds are configurable in config.json
  (`retention_days`, `cap_gib`) or per-run (`passband compost --days N`).

## Data layout

```
<data-dir>/                     default ~/.local/share/passband
  config.json                   lat/lon, retention, watch dir
  events/YYYY-MM-DD.jsonl       the truth
  index.sqlite3                 derived; delete freely
  grants.jsonl                  every tuner grant, with its reason
  snippets/*.sigmf-data/.meta   evidence, composted
  editions/<name>@<version>/    sealed capability + manifest.json
  receipts/<name>@<version>.json
  compost.jsonl                 what was composted and why
  run/                          decoder scratch (readsb JSON, SatDump out)
  hf-watch/                     default ALL.TXT watch directory
```

## Honest limits

- **Pass predictions are approximate.** The propagator is Kepler's
  equation plus J2 secular terms on a spherical Earth - not SGP4.
  Expect a few minutes of error in pass times, growing with the age
  of the elements. Every prediction Passband prints says so. It is
  enough to be waiting at 137 MHz; nothing is pointed by it.
- **No passive radar, no direction finding.** Both need two coherent
  channels at once; one dongle is one channel.
- **Weather radio is presence, not transcription.** Energy in the
  demodulated audio says a station is there; EAS/SAME headers, when
  multimon-ng is installed, are decoded properly as alert events.
- **SatDump pipeline ids vary by version.** The plan block carries
  the pipeline name (`metm2-3` by default) because SatDump renames
  pipelines between releases; set it to the id your SatDump knows.
- **ACARS text mode is a fallback.** Recent acarsdec builds print
  JSON and are parsed exactly; the classic text blocks are parsed
  tolerantly and keep their raw text in the event payload.
- **HF is a file watch in v1.** WSJT-X (or any tool you wire) does
  the decoding and writes the log; Passband turns the log into events.
  FT8/WSPR arrive as events with the decoder's own timestamps.

## Tests

```sh
python3 -m pytest
```

74 tests: the event contract, the store and its index rebuild, the
registry and its absent-regulars, every decoder parser against the
recorded fixtures in `fixtures/`, scheduler rotation and pass
preemption on a fake clock, the editions gate including tamper
refusal, snippet composting, pass sanity, and CLI smoke - all with
no network, no dongle, and no decoder binaries installed.
