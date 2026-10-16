"""The Group Pool: scheduling decisions and Child mechanics for parallel runs.

When ``--isolated-numprocesses`` is above 1 the Parent keeps up to N Child
processes alive at once instead of exactly one. This module owns three things:

- the pure admission decision (which Groups may launch this tick), so the
  scheduling rules -- slot accounting, Exclusive (serial) gating, the maxfail
  launch-gate, collection order -- are testable without spawning anything;
- the pure sweep decision (what happens to each live Child this tick), so the
  timeout/terminate-grace/reap policy -- the run's timeout contract -- is
  testable as a table of plain values on every platform. Spec:
  https://github.com/dyollb/pytest-isolated/issues/72
- the imperative Child mechanics: spawn with drained pipes (or inherited
  stdio when the Parent's capture is disabled), deadline-based timeout with
  manual partial-output capture (the Partial-output invariant under
  ``Popen``), and escalation kill for timeout and Interrupted paths.

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
from typing import BinaryIO, Final, NamedTuple, TypeAlias

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

# Bytes requested per incremental pipe read; any nonzero amount received is
# published immediately, so the size only bounds a single read call.
_READ_CHUNK_BYTES: Final = 65536


class SubprocessResult(NamedTuple):
    """Result from running a subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class ChildState(NamedTuple):
    """One Child's plain-value snapshot for the sweep decision.

    ``alive`` comes from one ``poll()`` per live Child per tick; ``deadline``
    and ``kill_requested`` are the Child's own monotonic clock fields (set
    when the Child was launched, and when a terminate was requested).
    """

    name: str
    alive: bool
    deadline: float
    kill_requested: float | None


class TerminateForTimeout(NamedTuple):
    """Decided action: mark the Group timed out and request terminate."""

    name: str


class EscalateToKill(NamedTuple):
    """Decided action: hard-kill a Child whose terminate grace has expired."""

    name: str


class Reap(NamedTuple):
    """Decided action: collect a finished Child's result and report it."""

    name: str


SweepAction: TypeAlias = TerminateForTimeout | EscalateToKill | Reap


def decide_sweeps(
    states: Sequence[ChildState],
    now: float,
    grace: float = _KILL_GRACE_SECONDS,
) -> list[SweepAction]:
    """Return this tick's actions for every Child, in running order.

    Pure policy (spec: https://github.com/dyollb/pytest-isolated/issues/72):
    plain values in, decided actions out -- no process, no clock, no
    filesystem. The caller applies each action through the Child's mechanics
    adapters. One Child's state never suppresses another's action: a sibling
    sitting in its terminate grace cannot stall anyone else's deadline check
    (the bug fixed by https://github.com/dyollb/pytest-isolated/commit/1d45d24).
    Reaps appear in running order so same-tick emission order stays stable.
    """
    actions: list[SweepAction] = []
    for st in states:
        if not st.alive:
            actions.append(Reap(st.name))
            continue
        if st.kill_requested is not None:
            if now - st.kill_requested >= grace:
                actions.append(EscalateToKill(st.name))
        elif now >= st.deadline:
            actions.append(TerminateForTimeout(st.name))
    return actions


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
        # Read incrementally (read1 yields whatever a single raw read
        # returns, never blocking for a full buffer) so a partial snapshot
        # holds the bytes already received even if EOF never arrives -- e.g.
        # a grandchild inherited the pipe and keeps it open after the Child
        # exits, leaving finish()'s capped thread joins short of EOF.
        read1 = getattr(stream, "read1", None) or stream.read
        while True:
            chunk = read1(_READ_CHUNK_BYTES)
            if not chunk:
                break
            self._chunks.append(chunk)

    def value(self) -> bytes:
        return b"".join(chunk for chunk in self._chunks if chunk)


class RunningChild:
    """One live Child: handle, drained-pipe buffers, deadline bookkeeping.

    ``capture=False`` (Parent runs with capture disabled, ``-s``) inherits the
    Parent's terminal instead of piping: output reaches the user live and
    interleaved, matching plain pytest's ``-s`` promise. The buffers stay
    empty in that mode, so failure notices carry no attached output -- the
    output already went to the terminal.
    """

    def __init__(
        self,
        name: str,
        spec: LaunchSpec,
        *,
        timeout: float,
        capture: bool = True,
    ) -> None:
        self.name = name
        self.spec = spec
        self.timed_out = False
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.kill_requested: float | None = None
        self._stdout = _PipeBuffer()
        self._stderr = _PipeBuffer()
        self.proc: subprocess.Popen[bytes] = subprocess.Popen(
            spec.cmd,
            env=spec.env,
            cwd=spec.cwd,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
        if not capture:
            self._out_thread = None
            self._err_thread = None
            return
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

    def sweep_state(self) -> ChildState:
        """Plain-value snapshot of this Child for ``decide_sweeps``.

        One ``poll()`` per tick; the clock fields stay owned by the Child.
        """
        return ChildState(
            name=self.name,
            alive=self.proc.poll() is None,
            deadline=self.deadline,
            kill_requested=self.kill_requested,
        )

    def request_kill(self) -> None:
        """Start a non-blocking escalating kill: terminate now, decide later.

        The sweep uses this instead of the blocking kill() so one stubborn
        Child's grace period can never delay the deadline checks (and kill
        requests) of its running siblings. Safe to call repeatedly or on an
        exited Child.
        """
        if self.proc.poll() is not None or self.kill_requested is not None:
            return
        self.proc.terminate()
        self.kill_requested = time.monotonic()

    def escalate_kill(self) -> None:
        """Hard-kill a Child whose request_kill() grace period has expired.

        Never waits; call every sweep tick. The Child stays in the running
        set until it is actually reaped, so the grace can end at any tick.
        """
        if self.kill_requested is None or self.proc.poll() is not None:
            return
        if time.monotonic() - self.kill_requested >= _KILL_GRACE_SECONDS:
            self.proc.kill()

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

    def drain_done(self) -> bool:
        """True when both reader threads have hit EOF: nothing left to drain.

        Cheap (no locks, no waits), safe to call every sweep tick. A Child
        whose pipes a grandchild inherited never reports done until the
        grandchild exits; the pool caps how long it waits for that.
        """
        return all(
            thread is None or not thread.is_alive()
            for thread in (self._out_thread, self._err_thread)
        )

    def finish(
        self, *, drain_timeout: float = _DRAIN_JOIN_TIMEOUT_SECONDS
    ) -> SubprocessResult:
        """Collect the finished Child's result; pipes drained into buffers.

        ``drain_timeout=0.0`` never waits: the buffers are published
        incrementally, so the snapshot holds every byte read so far even if a
        reader is still blocked on a pipe a grandchild kept open.
        """
        returncode = self.proc.poll()
        if returncode is None:  # pragma: no cover - caller reaps only after exit
            returncode = self.proc.wait()
        for thread in (self._out_thread, self._err_thread):
            if thread is not None:
                thread.join(timeout=drain_timeout)
        return SubprocessResult(
            returncode=returncode,
            stdout=self._stdout.value(),
            stderr=self._stderr.value(),
            timed_out=self.timed_out,
        )


def launch_child(
    name: str, spec: LaunchSpec, *, timeout: float, capture: bool = True
) -> RunningChild:
    """Spawn one planned Child with its timeout deadline armed."""
    return RunningChild(name, spec, timeout=timeout, capture=capture)


def poll_interval() -> float:
    """The pool's idle sleep between sweeps."""
    return _POLL_INTERVAL_SECONDS


def drain_join_timeout() -> float:
    """The cap on waiting for one Child's pipe drain before moving on.

    Past this budget the pool emits the Group with the partial output the
    readers already hold rather than let one Child's stuck pipes hold the
    scheduling loop (and every sibling's deadline) hostage.
    """
    return _DRAIN_JOIN_TIMEOUT_SECONDS
