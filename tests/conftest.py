"""Shared fixtures: a temp data dir, a store on it, a runner context."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from passband.decoders import RunnerContext  # noqa: E402
from passband.store import EventStore  # noqa: E402

BASE_TIME = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def store(data_dir):
    return EventStore(data_dir)


@pytest.fixture
def ctx(data_dir):
    return RunnerContext(data_dir=data_dir, edition="test")


@pytest.fixture
def sim_ctx(data_dir):
    return RunnerContext(
        data_dir=data_dir, edition="test", simulate=True, base_time=BASE_TIME
    )
