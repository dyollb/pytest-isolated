"""Unit tests for the Group Pool seams (spec issue #70).

Three pure/direct seams, following the repo's pattern of testing deep
interfaces with plain values (see tests/test_launch_plan.py,
tests/test_child_args.py):

- decide_admissions: the whole scheduling policy as a table-driven function,
  no Children spawned;
- resolve_numprocesses: the parallelism knob, with affinity monkeypatched;
- RunningChild kill lifecycle: real short-lived processes, asserted via
  poll() and drained buffers (Ctrl-C is not expressible through pytester).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from pytest_isolated.config import resolve_numprocesses
from pytest_isolated.launch import LaunchSpec
from pytest_isolated.pool import (
    _KILL_GRACE_SECONDS,
    RunningChild,
    _PipeBuffer,
    decide_admissions,
)


class TestDecideAdmissions:
    """The admission rules: slots, serial exclusivity, maxfail gate, order."""

    def test_maxfail_gate_launches_nothing(self) -> None:
        assert (
            decide_admissions(
                pending=["a", "b"],
                running_count=0,
                serial=frozenset(),
                max_parallel=4,
                maxfail_tripped=True,
            )
            == []
        )

    def test_respects_free_slots(self) -> None:
        admitted = decide_admissions(
            pending=["a", "b", "c", "d"],
            running_count=1,
            serial=frozenset(),
            max_parallel=3,
            maxfail_tripped=False,
        )
        assert admitted == ["a", "b"]  # 2 free slots, collection order

    def test_no_slots_admits_nothing(self) -> None:
        assert (
            decide_admissions(
                pending=["a"],
                running_count=2,
                serial=frozenset(),
                max_parallel=2,
                maxfail_tripped=False,
            )
            == []
        )

    def test_serial_head_admits_alone_when_idle(self) -> None:
        admitted = decide_admissions(
            pending=["solo", "a", "b"],
            running_count=0,
            serial=frozenset({"solo"}),
            max_parallel=4,
            maxfail_tripped=False,
        )
        assert admitted == ["solo"]  # exclusive: nothing joins it

    def test_serial_head_blocks_queue_while_pool_busy(self) -> None:
        # Regular groups behind the serial head must not cut in (no
        # starvation); the pool drains down to the serial Group.
        admitted = decide_admissions(
            pending=["solo", "a", "b"],
            running_count=1,
            serial=frozenset({"solo"}),
            max_parallel=4,
            maxfail_tripped=False,
        )
        assert admitted == []

    def test_serial_midqueue_blocks_and_regular_before_it_launch(self) -> None:
        # Regular groups ahead of the serial head keep the pool busy while
        # the serial Group waits for its natural collection-order slot.
        admitted = decide_admissions(
            pending=["a", "solo", "b"],
            running_count=0,
            serial=frozenset({"solo"}),
            max_parallel=4,
            maxfail_tripped=False,
        )
        assert admitted == ["a"]

    def test_serial_only_pending_admits_when_drained(self) -> None:
        admitted = decide_admissions(
            pending=["solo"],
            running_count=0,
            serial=frozenset({"solo"}),
            max_parallel=4,
            maxfail_tripped=False,
        )
        assert admitted == ["solo"]

    def test_collection_order_preserved(self) -> None:
        admitted = decide_admissions(
            pending=["z", "y", "x"],
            running_count=0,
            serial=frozenset(),
            max_parallel=3,
            maxfail_tripped=False,
        )
        assert admitted == ["z", "y", "x"]

    def test_exclusive_running_blocks_everything(self) -> None:
        # While a serial Child is alive, nothing else may launch even with
        # free slots and regular Groups pending.
        assert (
            decide_admissions(
                pending=["a", "b"],
                running_count=1,
                serial=frozenset({"solo"}),
                max_parallel=4,
                maxfail_tripped=False,
                exclusive_running=True,
            )
            == []
        )

    def test_serial_degenerate_one_slot_serial_order(self) -> None:
        # max_parallel=1 (serial mode): admits exactly the collection head.
        admitted = decide_admissions(
            pending=["first", "second"],
            running_count=0,
            serial=frozenset(),
            max_parallel=1,
            maxfail_tripped=False,
        )
        assert admitted == ["first"]


class TestResolveNumprocesses:
    def test_unset_means_serial(self) -> None:
        assert resolve_numprocesses(None) == 1
        assert resolve_numprocesses("") == 1
        assert resolve_numprocesses("  ") == 1

    def test_integer(self) -> None:
        assert resolve_numprocesses("8") == 8

    def test_auto_uses_affinity_on_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        if not hasattr(os, "sched_getaffinity"):  # pragma: no cover - Windows
            pytest.skip("POSIX-only affinity path")
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3})
        assert resolve_numprocesses("auto") == 4

    def test_auto_falls_back_to_cpu_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if hasattr(os, "sched_getaffinity"):
            monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set())
            monkeypatch.setattr(os, "cpu_count", lambda: 7)
        else:  # pragma: no cover - Windows
            monkeypatch.delattr(os, "process_cpu_count", raising=False)
            monkeypatch.setattr(os, "cpu_count", lambda: 7)
        assert resolve_numprocesses("auto") == 7

    def test_auto_survives_process_cpu_count_returning_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows-shaped branch: no affinity source, process_cpu_count()
        # answers None (undeterminable) -> must fall through to cpu_count.
        monkeypatch.delattr(os, "sched_getaffinity", raising=False)
        monkeypatch.setattr(os, "process_cpu_count", lambda: None, raising=False)
        monkeypatch.setattr(os, "cpu_count", lambda: 5)
        assert resolve_numprocesses("auto") == 5

    def test_auto_case_insensitive(self) -> None:
        assert resolve_numprocesses("AUTO") == resolve_numprocesses("auto")

    @pytest.mark.parametrize("bad", ["zero", "8.5", "-1", "0"])
    def test_invalid_values_raise_usage_error_naming_value(self, bad: str) -> None:
        with pytest.raises(pytest.UsageError) as excinfo:
            resolve_numprocesses(bad)
        assert repr(bad) in str(excinfo.value)


def _spec_for(code: str) -> LaunchSpec:
    """A LaunchSpec running inline Python in this interpreter."""
    return LaunchSpec(
        cmd=[sys.executable, "-u", "-c", code],
        env=dict(os.environ),
        cwd=str(Path.cwd()),
        report_path="/dev/null",  # RunningChild never touches it
    )


class TestRunningChildLifecycle:
    def test_finish_collects_exit_and_output(self) -> None:
        child = RunningChild(
            "g",
            _spec_for("print('hello'); import sys; print('err', file=sys.stderr)"),
            timeout=30,
        )
        while child.is_alive():
            time.sleep(0.01)
        result = child.finish()
        assert result.returncode == 0
        # On Windows, CPython text-mode stdout translates \n to \r\n when the
        # pipe is not a console; compare platform-normalized output.
        stdout = result.stdout.replace(b"\r\n", b"\n")
        stderr = result.stderr.replace(b"\r\n", b"\n")
        assert stdout == b"hello\n"
        assert stderr == b"err\n"
        assert result.timed_out is False

    def test_kill_terminates_and_leaves_no_live_process(self) -> None:
        child = RunningChild("g", _spec_for("import time; time.sleep(60)"), timeout=30)
        assert child.is_alive()
        child.kill()
        assert child.proc.poll() is not None  # exited, no orphan
        result = child.finish()
        assert result.timed_out is False  # kill itself does not set the flag

    def test_request_kill_returns_immediately_and_terminates(self) -> None:
        child = RunningChild("g", _spec_for("import time; time.sleep(60)"), timeout=30)
        try:
            start = time.monotonic()
            child.request_kill()
            # Must not block for the grace period (unlike kill(), which
            # costs >= 5s on a sleeper that never exits); 3s leaves room
            # for a loaded CI runner without weakening the claim.
            assert time.monotonic() - start < 3.0
            deadline = time.monotonic() + 5
            while child.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert child.proc.poll() is not None
        finally:
            child.kill()

    @pytest.mark.skipif(sys.platform == "win32", reason="needs an ignorable SIGTERM")
    def test_escalate_kill_finishes_a_stubborn_child(self) -> None:
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('armed', flush=True)\n"
            "time.sleep(60)\n"
        )
        child = RunningChild("g", _spec_for(code), timeout=30)
        try:
            # Only fight after the handler is installed: a still-starting
            # interpreter takes SIGTERM like any process.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not child._stdout.value():
                time.sleep(0.01)
            assert child._stdout.value(), "child never armed"
            child.request_kill()
            time.sleep(0.3)
            assert child.is_alive()  # terminate() was ignored
            child.escalate_kill()  # grace not expired: no-op, never waits
            assert child.is_alive()
            # Age the request past the grace without sleeping for it.
            child.kill_requested -= _KILL_GRACE_SECONDS + 1
            child.escalate_kill()
            deadline = time.monotonic() + 5
            while child.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert child.proc.poll() is not None
        finally:
            child.kill()

    def test_kill_on_exited_child_is_noop(self) -> None:
        child = RunningChild("g", _spec_for("pass"), timeout=30)
        while child.is_alive():
            time.sleep(0.01)
        child.kill()  # must not raise
        assert child.finish().returncode == 0

    def test_partial_output_survives_kill(self) -> None:
        """The Partial-output invariant under Popen: killed Children keep
        everything printed so far (unbuffered -u + drained pipes)."""
        code = "import time; print('early output', flush=True); time.sleep(60)"
        child = RunningChild("g", _spec_for(code), timeout=30)
        # Give the Child time to print, then kill it mid-run.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child._stdout.value():
                break
            time.sleep(0.01)
        child.kill()
        result = child.finish()
        assert b"early output" in result.stdout.replace(b"\r\n", b"\n")


class TestPipeBufferIncremental:
    def test_publishes_bytes_before_eof(self) -> None:
        """consume() must make received bytes visible without waiting for EOF.

        A grandchild keeping the write end open would otherwise starve the
        read; the reader thread publishes each read1() chunk as it arrives.
        """
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=-1)  # BufferedReader
        buffer = _PipeBuffer()
        thread = threading.Thread(target=buffer.consume, args=(stream,), daemon=True)
        thread.start()
        try:
            os.write(write_fd, b"first chunk")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not buffer.value():
                time.sleep(0.01)
            # Visible while the writer is still open (no EOF yet).
            assert buffer.value() == b"first chunk"
            os.write(write_fd, b" + second")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and buffer.value() == b"first chunk":
                time.sleep(0.01)
            assert buffer.value() == b"first chunk + second"
        finally:
            os.close(write_fd)  # EOF lets the reader thread exit
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert buffer.value() == b"first chunk + second"
