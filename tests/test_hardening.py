"""Tuner hardening: process groups, bounded waits, holder reports.

The tuner is one device. A decoder child that outlives its grant -
or a grandchild nobody signalled - holds it hostage, so these tests
prove the supervision: children run in their own process groups,
stopping ends the whole group even when TERM is ignored, every wait
a runner sits in has a deadline behind it, and a normal run's
lifecycle is unchanged.
"""

import os
import subprocess
import sys
import threading
import time

from passband import decoders, tuner
from passband.decoders import Runner, RunnerContext
from passband.events import make_event


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


# -- process groups ---------------------------------------------------


def test_stop_all_ends_the_whole_group(tmp_path):
    """A child and the grandchild IT spawned both end at _stop_all."""
    pids_file = tmp_path / "pids.txt"
    script = (
        "import subprocess, time\n"
        f"grandchild = subprocess.Popen(['sleep', '300'])\n"
        f"open({str(pids_file)!r}, 'w').write(str(grandchild.pid))\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script],
                            start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not pids_file.is_file():
            assert time.monotonic() < deadline, "child never reported"
            time.sleep(0.05)
        grandchild_pid = int(pids_file.read_text().strip())
        assert _alive(proc.pid) and _alive(grandchild_pid)
        Runner._stop_all([proc])
        assert _wait_dead(proc.pid)
        assert _wait_dead(grandchild_pid), "grandchild survived _stop_all"
    finally:
        if proc.poll() is None:
            proc.kill()


class _StubbornRunner(Runner):
    """A decoder that ignores SIGTERM and never exits on its own."""

    name = "stubborn"
    mode = "test"

    def __init__(self):
        super().__init__(binary=sys.executable)
        self.procs: list = []

    def build_argv(self, ctx):
        return [sys.executable, "-c",
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(300)"]

    def _spawn(self, ctx):
        self.procs = super()._spawn(ctx)
        return self.procs


def test_hung_runner_is_ended_on_time(tmp_path):
    """run() returns shortly after its deadline; no survivor remains."""
    runner = _StubbornRunner()
    ctx = RunnerContext(data_dir=tmp_path)
    started = time.monotonic()
    result = runner.run(ctx, lambda event: None, threading.Event(),
                        duration=1.0)
    elapsed = time.monotonic() - started
    assert result["ran"] is True
    # 1 s grant + the bounded TERM grace before KILL lands.
    assert elapsed < 25, f"hung runner took {elapsed:.1f}s to end"
    assert runner.procs and _wait_dead(runner.procs[0].pid)


class _EchoRunner(Runner):
    name = "echo"
    mode = "test"

    def __init__(self):
        super().__init__(binary=sys.executable)

    def build_argv(self, ctx):
        return [sys.executable, "-c", "print('one')\nprint('two')"]

    def parse_line(self, line, ctx):
        if not line.strip():
            return []
        return [make_event(mode="test", decoder=self.name,
                           edition=ctx.edition, freq_hz=100_000_000,
                           payload={"line": line.strip()},
                           ts=ctx.stamp())]


def test_normal_runner_lifecycle_unchanged(tmp_path):
    """A well-behaved decoder runs, is heard, and ends as before."""
    runner = _EchoRunner()
    ctx = RunnerContext(data_dir=tmp_path)
    heard = []
    started = time.monotonic()
    result = runner.run(ctx, heard.append, threading.Event(),
                        duration=30.0)
    elapsed = time.monotonic() - started
    assert result["ran"] is True
    assert result["events"] == 2
    assert [event.payload["line"] for event in heard] == ["one", "two"]
    assert elapsed < 10, "a finished decoder should end the run at once"


# -- bounded reads ------------------------------------------------------


def test_wxradio_hung_rtl_fm_still_ends(tmp_path, monkeypatch):
    """rtl_fm alive but silent forever: the grant still ends on time.

    The weather-radio loop once waited in a bare blocking pipe read;
    a hung rtl_fm would have held the tuner past any deadline.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "rtl_fm"
    fake.write_text("#!/bin/sh\nsleep 300\n")
    fake.chmod(0o755)
    monkeypatch.setenv(
        "PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    runner = decoders.WxRadioRunner()
    ctx = RunnerContext(data_dir=tmp_path)
    started = time.monotonic()
    result = runner.run(ctx, lambda event: None, threading.Event(),
                        duration=1.0)
    elapsed = time.monotonic() - started
    assert result["ran"] is True
    assert elapsed < 15, f"hung rtl_fm held the grant for {elapsed:.1f}s"


# -- stale holder reports -----------------------------------------------


def _proc_entry(root, pid, argv):
    entry = root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(
        b"\0".join(part.encode() for part in argv) + b"\0")


def test_tuner_holders_reports_decoders_only(tmp_path):
    _proc_entry(tmp_path, 4242, ["/usr/bin/rtl_fm", "-f", "162550000"])
    _proc_entry(tmp_path, 4343, ["/bin/bash", "-l"])
    _proc_entry(tmp_path, 4444, ["readsb", "--quiet"])
    holders = tuner.tuner_holders(proc_root=tmp_path, own_pid=999999)
    assert [holder["pid"] for holder in holders] == [4242, 4444]
    assert "readsb" in holders[1]["command"]


def test_tuner_holders_excludes_own_process(tmp_path):
    _proc_entry(tmp_path, 4242, ["rtl_airband", "-F", "-e"])
    holders = tuner.tuner_holders(proc_root=tmp_path, own_pid=4242)
    assert holders == []
