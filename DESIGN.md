# Passband - Design

*by Free Computation. License: AGPL-3.0. Status: design, pre-build. This document is the contract the first build answers to.*

## What Passband is

Passband is the ear of Fixity. Coffer carries the world's knowledge aboard so a person can read with no connection. Passband does the same for the radio spectrum around the person: it listens with one cheap receiver, decodes what it hears into events, and keeps a local, queryable record of the invisible traffic of a place - aircraft overhead, weather satellites passing, ships, sensors, pagers, the weather radio - with no network, no account, and no telemetry.

The name is the mechanism. A passband is the slice of spectrum a receiver admits and keeps; this system is receive-only, admits a slice of the air, and keeps what it hears. Fixity is the system, Coffer is the chest that carries its cargo, Passband is how it hears.

Passband is a Free Computation project. It stands under the Fixity charter and its laws, applied to spectrum:

- Sustainment (charter law 1). Networking is included and nothing depends on it. Every function - tuning, decoding, storing, querying, reporting - works at zero connectivity, indefinitely. A connection, when one exists, is only a supply line for editions, verified at the same gate as a capsule carried by hand.
- Growth (charter law 2). Passband is not shipped with every decoder it will ever run. Decoders and reference data arrive as editions and grown structures, each carrying its receipt - the need that called it, what it was made from, the hash it answers to - and each passing the growth gate before it runs with authority.
- Editions, not updates (charter law 5). Nothing is ever pushed to a running Passband. New capability is shelved as a sealed, content-addressed edition or not at all.
- The core stays small (charter law 6). The trusted core - tuner control, the scheduler, the event store, the gate - stays small enough for one person to read end to end. Decoders are cargo, not core.
- No accounts, no telemetry (charter law 8). Passband never transmits. It is receive-only by construction: there is no transmit code path anywhere in the design, in the core or in any decoder it will shelve. What a person's Passband hears belongs to that person and stays on their machine.

## The hardware truth

The design is built on one receiver and a computer the owner already has. The budget is $0 and the bill of materials is closed:

- One RTL-SDR Blog V4 - RTL2832U with the R828D tuner, 500 kHz to 1.766 GHz, 1 ppm TCXO, bias tee, and a built-in HF upconverter (28.8 MHz local oscillator) behind a triplexer with AM, FM and DAB notch filters. HF is heard through the upconverter, so there is no direct-sampling fold and no sensitivity collapse below 14 MHz, and tuner gain works on HF.
- Its stock telescopic dipole kit, re-lengthened per band as the day plan requires.
- A computer the owner already owns, running a free operating system.

Two hardware facts shape everything below:

- The V4 needs the current RTL-SDR Blog drivers. Stock librtlsdr builds do not switch the triplexer and upconverter correctly. Driver installation is step one of any setup, named and checked, never assumed.
- The V4 is end-of-line. RTL-SDR ended V4 production in May 2026 when its stockpile of R828D tuner chips ran out. Passband therefore targets the RTL2832U family, not the V4 alone: the reference rig is the V4, and the unit another person can still buy new is the Blog V3, which remains in stable production. Every part of the design must run on either, with the V3's HF direct-sampling limits stated where they differ.

The receiver's hard limits, stated once and designed around rather than wished away: 8-bit samples, roughly 48 dB of usable dynamic range; a stable sample rate of 2.4 MS/s, so about 2.4 MHz of spectrum visible at any instant; and one tuner, which can be in exactly one place at a time.

## The one hard problem: one tuner, and the cost of raw spectrum

Continuous raw capture is not a design, it is a flood. At 2.4 MS/s with 8-bit I/Q, the dongle produces 4.8 MB every second - about 17.3 GB an hour, about 0.41 TB a day - of mostly nothing. Storing it is impossible on purpose-sized hardware and useless besides: nobody can read a flood.

So Passband's centre is not a recorder. It is two things:

- An event store. Decoders run at the edge, at the moment of reception, and what is kept is the decoded event - an aircraft position, a weather image line, a sensor reading, a pager message - each stamped with time, frequency, mode, signal strength, and the decoder and edition that produced it. Events are small, structured, and kept forever by default. This is the listening-post pattern, proven elsewhere: a registry of what was heard, when, and how it behaves over time.
- A scheduler. One tuner cannot hear everything at once, so Passband plans its listening the way a person would: it knows when a weather satellite will pass (predicted locally from orbital elements carried aboard as an edition), when the house's sensors speak, and what the owner cares about most, and it moves the tuner to be in the right place before the moment comes. What goes dark while the tuner is elsewhere is stated in the plan, never hidden. A plan is a published manifest of attention, and the owner edits it.

Raw spectrum is kept only as evidence: when a decoder or a trigger fires, Passband may keep a short capture around the event in SigMF format - seconds, not hours - so a surprising signal can be re-examined. Everything else is discarded as it flows past. The discipline is: decode at the edge, keep the event, keep evidence briefly, never keep the flood.

## Architecture

The core is small and fixed. Everything else is cargo that arrives by edition and can be composted.

- Tuner seam. The one place that talks to the hardware: open the dongle through the Blog drivers, set frequency, sample rate, gain and bias tee, stream samples to exactly one consumer at a time. It knows the difference between a V4 and a V3 and says so in its receipts. Nothing else in the system touches USB.
- Scheduler. Owns the tuner by time. Holds the day plans, the pass predictions, and the owner's priorities; grants the tuner to one listener at a time; logs every grant, so the record can always answer "why were we not listening to X at time T" with "we were on Y, by this plan".
- Decoder runners. Thin supervision around external decoder programs: start one against the tuner stream or a captured snippet, parse its output into events, stop it cleanly. A decoder is never trusted with the store directly; the runner validates and stamps every event.
- Event store. Append-only, local, in plain documented formats that survive the program: one event per line of JSON in dated files, plus a small index the system grows for itself. The store is the product. Every view Passband shows is a query over it.
- Registry. The standing picture the events accumulate into: each emitter or service heard - an aircraft, a satellite, a sensor, a station - with first heard, last heard, count, usual hours, and a confidence grade for what it is believed to be. Pattern of life is a query over the registry, not a separate system.
- Report surface. How the owner reads the ear: a daily summary (what passed overhead, what was new, what was unusually absent), live view while a listener runs, and free query over the store. In Fixity's first form this surface is pages Coffer can render; standalone, it is plain files and a small local page.

Recommended core language: Python, one readable codebase, standard library first. The reasons are the charter's: the core must be readable end to end by its owner, the owner is learning Python deliberately, and the heavy work already lives in the external decoders, which are other people's proven C programs. The core's job is scheduling, supervision, and bookkeeping - judgement work, not sample work. If a piece ever needs to be fast, it can be regrown in a faster language under the growth law, with its receipt saying why.

## Decoders and reference data: capability as editions

Passband invents no decoders. The free software world already maintains excellent ones, and Passband's work is the scheduling, the store, and the gate - the same division of labour as Coffer, whose work is the browser and the curation, not re-scraping the world. Each decoder below is free and open source, runs fully offline, and is shelved as an edition with a receipt before it runs:

- Aircraft: readsb for ADS-B at 1090 MHz - positions, identities, velocities, the sky written as events.
- Aircraft voice: RTLSDR-Airband on the 118-137 MHz airband - every channel inside one tuned slice demodulated at once; each transmission is kept as a clip and logged as a voice event naming its channel. Passband never guesses who spoke. It does transcribe clips after the fact, in the triage stage, for one purpose only: judging whether anyone spoke at all - and when it cannot judge, the clip is untriaged, never guessed.
- Sensors and the house: rtl_433 at 433 and 915 MHz - weather stations, tyre sensors, utility meters, the machine chatter of a neighbourhood.
- Aircraft voice data links: acarsdec and dumpvdl2 for ACARS and VDL2.
- Weather satellites: SatDump for Meteor-M LRPT at 137 MHz, decoded to images - scheduled by pass prediction, the scheduler's proving ground. (NOAA APT is gone: NOAA-15, 18 and 19 were decommissioned in the summer of 2025. The design carries no dead capability.)
- Weather radio and broadcast: NOAA Weather Radio at 162 MHz and ordinary FM, heard as audio and logged as presence.
- Ships, where geography allows: AIS-catcher at 162 MHz.
- Pagers: multimon-ng for POCSAG.
- HF, at night through the V4's upconverter: FT8 and WSPR spots, WeFax charts, and HFDL, on the decoder set SatDump and the HF tools provide.

Reference data is cargo under the same rule - sealed editions, verified, shelved, usable forever offline:

- Orbital elements (TLEs) for the satellites Passband predicts passes for.
- The aircraft database that turns an ADS-B hex code into a type and a registration.
- Band plans and the rtl_433 device knowledge that turns a burst into a named sensor.
- Each edition states its date and its source. Stale data is labelled stale in every report that uses it; a pass prediction made from old elements says so.

## What Passband is not

Stated plainly, so the design never quietly promises them:

- Not a transmitter, a jammer, or an interceptor of people. Receive-only, public and broadcast signals, the owner's own environment.
- Not passive radar and not direction finding. Those need two coherent channels at once; one dongle is one channel. They stay out of reach until different hardware exists, and the design does not pretend otherwise.
- Not a raw-IQ archive, and not a machine-learning lab. Classification lives in the named decoders. If evidence grading over the registry grows later, it grows as an edition with receipts, over events - never as a model swallowing the flood.
- Not a network service. Passband may one day feed its summaries to Coffer as a local page; it never phones home, and no one else's Passband is visible to yours.

## Build order

Each step ends with something the owner can run and hear, and each is verified on the real V4 before the next begins.

- v0 - First hearing. The tuner seam and one decoder runner, ADS-B via readsb, events landing in the store, a report that lists aircraft heard. Acceptance: with the network cable pulled, Passband on the V4 hears aircraft overhead and the store answers who, when, and how strong - and the core is still readable in one sitting.
- v1 - The scheduler. Day plans, dwell, priorities, the grant log. Acceptance: one unattended day runs a published plan end to end and the report states what went dark and when, by the plan's own word.
- v2 - Passes. The TLE edition, pass prediction, SatDump, Meteor-M. Acceptance: Passband predicts a pass from aboard data, is waiting at 137 MHz when it comes, and shelves a weather image - again with the cable pulled.
- v3 - The registry. Emitters accumulate pattern of life; rtl_433 joins as the second family; the daily summary names what was new and what was missing. Acceptance: after a week unattended, the summary correctly names a regular that failed to appear.
- After v3: ACARS, weather radio logging, POCSAG, AIS where geography allows, HF nights - each one edition at a time, each with its receipt, none of them owed by the core.

## Open rulings

- Core language: Python is recommended above; the ruling is the owner's.
- First decoder: ADS-B is recommended for v0 (constant traffic, instant verification against the visible sky); rtl_433 is the alternative (the house itself, quieter but nearer).
- Evidence retention: how many days of triggered SigMF snippets the store keeps before composting - a size in gigabytes, stated in the plan.
- First form: standalone on the owner's existing machine first, as Coffer ships standalone before the system - recommended, and the owner's to change.

Settled (2026-11-09, on the owner's go-ahead to build the whole thing): the core is Python, standard library only. ADS-B is in the first decoder set, first among equals. Evidence retention defaults to 7 days and a 2 GiB cap, both configurable. The first form is standalone. Pass prediction in v1 uses a Kepler + J2 approximation rather than SGP4 and is labelled approximate wherever it appears.

Settled (2026-11-10, the owner's improvement round): airband clips are triaged. A `triage` stage judges each clip voice, static, or untriaged, from transcription by the optional whisper stack (faster-whisper with a local model - an external tool beside the standard-library core, like a decoder binary). Transcription is never given an initial prompt: a prompt describing aviation speech makes the model hallucinate the prompt's own phrases over pure static, measured on this station's own clips. Acoustic features are recorded as evidence but never decide a verdict. Static clips are composted past a retention age (config `triage_retention_days`, default 7) only when the owner runs triage with --compost; voice and untriaged clips are never composted. The daily report carries the triage counts and names the day's voice clips.
