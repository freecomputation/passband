"""Passband - the ear of Fixity.

A receive-only, offline-first listening system for one RTL-SDR dongle.
Passband schedules one tuner across proven external decoders, keeps what it
hears as events in a local append-only store, and builds a registry of
the emitters around a place. It never transmits, has no accounts, no
telemetry, and no network client code: the only thing it reads besides
the tuner is local files.
"""

__version__ = "0.1.0"
