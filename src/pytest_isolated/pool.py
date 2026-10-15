"""The Group Pool: scheduling decisions and Child mechanics for parallel runs.

When ``--isolated-numprocesses`` is above 1 the Parent keeps up to N Child
processes alive at once instead of exactly one. This module owns two things:

- the pure admission decision (which Groups may launch this tick), so the
  scheduling rules -- slot accounting, Exclusive (serial) gating, the maxfail
  launch-gate, collection order -- are testable without spawning anything;
- the imperative Child mechanics: spawn with drained pipes, deadline-based
  timeout with manual partial-output capture (the Partial-output invariant
  under ``Popen``), and escalation kill for timeout and Interrupted paths.

Deliberately stdlib-only (subprocess + threads + monotonic clock): the pool
stays Windows-safe by construction, the same reason the plugin exists instead
of pytest-xdist. Report emission never happens here -- the run loop reaps and
emits on the Parent's main thread.

Spec: https://github.com/dyollb/pytest-isolated/issues/70.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import BinaryIO, Final, NamedTuple

from .launch import LaunchSpec

# Grace period a Child gets between terminate() and kill().
_KILL_GRACE_SECONDS: Final = 5.0

# How long the pool sleeps between sweeps when there is nothing to do. Small
# enough for responsive timeouts, large enough to stay off the CPU.
_POLL_INTERVAL_SECONDS: Final = 0.05

# Upper bound on joining reader threads after a Child exits. A grandchild
# inheriting the pipes could block the read forever; the timed join degrades
# to "partial output we have" instead of hanging the Parent.
_DRAIN_JOIN_TIMEOUT_SECONDS: Final = 10.0


class SubprocessResult(NamedTuple):
    """Result from running a subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def decide_admissions(
    *,
    pending: Sequence[str],
    running_count: int,
    serial: AbstractSet[str],
    max_parallel: int,
    maxfail_tripped: bool,
    exclusive_running: bool = False,
) -> list[str]:
    """Return the Groups to launch now, in order, following three rules.

    1. maxfail gate: once tripped, nothing new launches; in-flight Children
       drain normally (pytest-xdist's shutdown semantics). Same for an
       already-running Exclusive Child (exclusive_running).
    2. Slot accounting: at most ``max_parallel - running_count`` launches.
    3. Exclusive (serial) Groups: a serial Group is admitted only when the
       Pool is otherwise idle, and it launches alone this tick. A serial
       Group blocks the queue behind it (FIFO), so it can never starve while
       regular Groups keep cutting in.
    """
    if maxfail_tripped or exclusive_running:
        return []
    free = max_parallel - running_count
    if free <= 0:
        return []

    admitted: list[str] = []
    for name in pending:
        if name in serial:
            if running_count == 0 and not admitted:
                admitted.append(name)
            break  # serial Groups block the queue (no starvation)
        admitted.append(name)
        free -= 1
        if free <= 0:
            break
    return admitted


class _PipeBuffer:
    """Byte sink filled by a reader thread until the pipe reaches EOF."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def consume(self, stream: BinaryIO) -> None:
        self._chunks.append(stream.read())

    def value(self) -> bytes:
        return b"".join(chunk for chunk in self._chunks if chunk)


class RunningChild:
    """One live Child: handle, drained-pipe buffers, deadline bookkeeping."""

    def __init__(self, name: str, spec: LaunchSpec, *, timeout: float) -> None:
        self.name = name
        self.spec = spec
        self.timed_out = False
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self._stdout = _PipeBuffer()
        self._stderr = _PipeBuffer()
        self.proc: subprocess.Popen[bytes] = subprocess.Popen(
            spec.cmd,
            env=spec.env,
            cwd=spec.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._out_thread = threading.Thread(
            target=self._stdout.consume,
            args=(self.proc.stdout,),
            daemon=True,
        )
        self._err_thread = threading.Thread(
            target=self._stderr.consume,
            args=(self.proc.stderr,),
            daemon=True,
        )
        self._out_thread.start()
        self._err_thread.start()

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def deadline_expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def kill(self) -> None:
        """Escalating kill: terminate, grace period, then kill outright.

        Safe to call on an already-exited Child (no-op). After a kill the
        exit status is signal-flavored; callers classify the Group from the
        path taken (timeout/Interrupted), never from the return code.
        """
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=_KILL_GRACE_SECONDS)

    def finish(self) -> SubprocessResult:
        """Collect the finished Child's result; pipes drained into buffers."""
        returncode = self.proc.poll()
        if returncode is None:  # pragma: no cover - caller reaps only after exit
            returncode = self.proc.wait()
        self._out_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        self._err_thread.join(timeout=_DRAIN_JOIN_TIMEOUT_SECONDS)
        return SubprocessResult(
            returncode=returncode,
            stdout=self._stdout.value(),
            stderr=self._stderr.value(),
            timed_out=self.timed_out,
        )


def launch_child(name: str, spec: LaunchSpec, *, timeout: float) -> RunningChild:
    """Spawn one planned Child with its timeout deadline armed."""
    return RunningChild(name, spec, timeout=timeout)


def poll_interval() -> float:
    """The pool's idle sleep between sweeps."""
    return _POLL_INTERVAL_SECONDS
