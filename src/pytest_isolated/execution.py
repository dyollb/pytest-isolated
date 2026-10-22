"""Subprocess execution for pytest-isolated."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Final, Literal, NamedTuple, TypeAlias, cast

import pytest
from _pytest.outcomes import Exit

from .config import (
    CONFIG_ATTR_GROUP_SERIAL,
    CONFIG_ATTR_GROUP_TIMEOUTS,
    CONFIG_ATTR_GROUPS,
    DEFAULT_TIMEOUT,
    SUBPROC_ENV,
    resolve_numprocesses,
)
from .launch import (
    GroupTempRoot,
    Hardened,
    LaunchSpec,
    open_group_temp_root,
    plan_launch,
)
from .parent_fixtures import (
    _PARENT_FIXTURES_PYTEST_VERSION_WARNING,
    _check_per_child_session_fixtures,
    _pytest_supports_parent_fixtures,
    _report_parent_fixture_outcome,
    _setup_parent_session_fixtures,
    _teardown_parent_session_fixtures,
)
from .pool import (
    EscalateToKill,
    Reap,
    RunningChild,
    SubprocessResult,
    TerminateForTimeout,
    decide_admissions,
    decide_sweeps,
    drain_join_timeout,
    launch_child,
    poll_interval,
)
from .reporting import (
    _emit_failure_for_items,
    _emit_report,
    _format_crash_message,
    _get_xfail_reason,
    _TestRecord,
)

# Type aliases for clarity
Phase: TypeAlias = Literal["setup", "call", "teardown"]
TestResults: TypeAlias = dict[str, dict[str, _TestRecord]]

_PARALLEL_NO_CAPTURE_WARNING: Final = (
    "pytest-isolated: running Child processes concurrently with capture "
    "disabled (-s / --capture=no); Child output goes straight to this "
    "terminal and will interleave. Without -s, Child output is replayed "
    "grouped per test (see pytest's -rA / -rf for always-visible grouped "
    "output)."
)


class ExecutionContext(NamedTuple):
    """Context for test execution."""

    session: pytest.Session


def _parse_results(report_path: str) -> TestResults:
    """Parse JSONL results file into dict[nodeid][phase] structure."""
    results: TestResults = {}
    report_file = Path(report_path)
    if report_file.exists():
        with report_file.open(encoding="utf-8") as f:
            for line in f:
                file_line = line.strip()
                if not file_line:
                    continue
                rec = cast(_TestRecord, json.loads(file_line))
                nodeid = rec["nodeid"]
                when = rec["when"]

                if nodeid not in results:
                    results[nodeid] = {}
                results[nodeid][when] = rec
        with contextlib.suppress(OSError):
            report_file.unlink()
    return results


def _handle_xfail_crash(
    returncode: int,
    results: TestResults,
    group_items: list[pytest.Item],
    ctx: ExecutionContext,
) -> bool:
    """Check if crash should be treated as xfail. Returns True if handled."""
    if returncode < 0 and results:
        # Check if all tests in this group are marked xfail
        all_xfail = all(it.get_closest_marker("xfail") for it in group_items)
        if all_xfail:
            # Override any results from subprocess - crash is the expected outcome
            msg = (
                f"Subprocess crashed with signal {-returncode} "
                f"(expected for xfail test)"
            )
            _emit_failure_for_items(group_items, msg, ctx.session)
            return True
    return False


def _handle_timeout(
    *,
    timed_out: bool,
    group_name: str,
    group_timeout: int,
    execution_time: float,
    group_items: list[pytest.Item],
    ctx: ExecutionContext,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> bool:
    """Handle subprocess timeout. Returns True if handled."""
    if timed_out:
        msg = (
            f"Subprocess group={group_name!r} timed out after {group_timeout} "
            f"seconds (execution time: {execution_time:.2f}s). "
            f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
            f"or @pytest.mark.isolated(timeout=N)."
        )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        _emit_failure_for_items(group_items, msg, ctx.session, stdout_text, stderr_text)
        return True
    return False


def _handle_collection_crash(
    *,
    returncode: int,
    results: TestResults,
    group_name: str,
    proc_stderr: bytes,
    group_items: list[pytest.Item],
    ctx: ExecutionContext,
) -> bool:
    """Handle crash during collection (no results produced). Returns True if handled."""
    if not results:
        stderr_text = proc_stderr.decode("utf-8", errors="replace").strip()
        msg = (
            f"Subprocess group={group_name!r} exited with code {returncode} "
            f"and produced no per-test report. The subprocess may have "
            f"crashed during collection."
        )
        if stderr_text:
            msg += f"\n\nSubprocess stderr:\n{stderr_text}"
        _emit_failure_for_items(group_items, msg, ctx.session)
        return True
    return False


def _detect_crashed_tests(
    group_items: list[pytest.Item],
    results: TestResults,
) -> tuple[list[pytest.Item], list[pytest.Item]]:
    """Detect crashed and not-run tests. Returns (crashed_items, not_run_items)."""
    crashed_items: list[pytest.Item] = []

    for it in group_items:
        node_results = results.get(it.nodeid, {})
        # Test started (setup passed) but crashed before call completed.
        # If setup was skipped or failed, no call phase is expected.
        if node_results and "call" not in node_results:
            setup_result = node_results.get("setup", {})
            setup_outcome = setup_result.get("outcome", "")
            if setup_outcome == "passed":
                crashed_items.append(it)

    # If we detected crashed tests, also find tests that never ran
    # (they come after the crashing test in the same group)
    not_run_items: list[pytest.Item] = []
    if crashed_items:
        for it in group_items:
            node_results = results.get(it.nodeid, {})
            # Test never started (no results at all)
            if not node_results:
                not_run_items.append(it)

    return crashed_items, not_run_items


def _handle_mid_test_crash(
    returncode: int,
    proc_stderr: bytes,
    group_items: list[pytest.Item],
    results: TestResults,
    ctx: ExecutionContext,
) -> bool:
    """Handle crash during test execution. Returns True if handled."""
    crashed_items, not_run_items = _detect_crashed_tests(group_items, results)

    if not (crashed_items or not_run_items):
        return False

    stderr_text = proc_stderr.decode("utf-8", errors="replace").strip()

    # Emit failures for crashed tests
    if crashed_items:
        crash_msg = _format_crash_message(
            returncode, "during test execution", stderr_text
        )

        for it in crashed_items:
            node_results = results.get(it.nodeid, {})
            # Emit setup phase if it was recorded
            if "setup" in node_results:
                rec = node_results["setup"]
                _emit_report(
                    it,
                    when="setup",
                    outcome=rec["outcome"],
                    longrepr=rec.get("longrepr", ""),
                    duration=rec.get("duration", 0.0),
                )
            else:
                _emit_report(
                    it,
                    when="setup",
                    outcome="passed",
                )

            # Emit call phase as failed with crash info
            xfail_reason = _get_xfail_reason(it)
            if xfail_reason:
                _emit_report(
                    it,
                    when="call",
                    outcome="skipped",
                    longrepr=crash_msg,
                    wasxfail=xfail_reason,
                )
            else:
                _emit_report(
                    it,
                    when="call",
                    outcome="failed",
                    longrepr=crash_msg,
                )
                ctx.session.testsfailed += 1

            _emit_report(
                it,
                when="teardown",
                outcome="passed",
            )
            # Remove from results so they're not processed again
            results.pop(it.nodeid, None)

    # Emit failures for tests that never ran due to earlier crash
    if not_run_items:
        not_run_msg = _format_crash_message(
            returncode, "during earlier test execution", stderr_text
        )
        not_run_msg = f"Test did not run - {not_run_msg}"
        _emit_failure_for_items(not_run_items, not_run_msg, ctx.session)
        for it in not_run_items:
            results.pop(it.nodeid, None)

    return True


def _emit_all_results(
    group_items: list[pytest.Item],
    results: TestResults,
    ctx: ExecutionContext,
) -> None:
    """Emit per-test results for all test phases."""
    phases: list[Phase] = ["setup", "call", "teardown"]

    for it in group_items:
        node_results = results.get(it.nodeid, {})

        # Skip tests that were already handled by crash detection
        if it.nodeid not in results:
            continue

        # Check if setup passed (to determine if missing call is expected)
        setup_passed = (
            "setup" in node_results and node_results["setup"]["outcome"] == "passed"
        )

        # Emit setup, call, teardown in order
        for when in phases:
            if when not in node_results:
                # If missing call phase AND setup passed, emit a failure
                # (crash detection should handle most cases, but this
                # is a safety net for unexpected situations)
                # If setup failed, missing call is expected (pytest skips call)
                if when == "call" and setup_passed:
                    msg = f"Missing 'call' phase result from subprocess for {it.nodeid}"
                    _emit_report(
                        it,
                        when="call",
                        outcome="failed",
                        longrepr=msg,
                    )
                    ctx.session.testsfailed += 1
                continue

            rec = node_results[when]
            _emit_report(
                it,
                when=when,
                outcome=rec.get("outcome", "failed"),
                longrepr=rec.get("longrepr", ""),
                duration=rec.get("duration", 0.0),
                stdout=rec.get("stdout", ""),
                stderr=rec.get("stderr", ""),
                sections=rec.get("sections"),
                user_properties=rec.get("user_properties"),
                wasxfail=rec.get("wasxfail"),
            )

            if when == "call" and rec["outcome"] == "failed":
                ctx.session.testsfailed += 1


def pytest_runtestloop(session: pytest.Session) -> int | None:
    """Execute isolated test groups in Child processes and remaining tests
    in-process.

    Subprocess timeouts are caught and reported as test failures; the
    timeout itself is never propagated. A KeyboardInterrupt instead kills
    every live Child, reports its Group as Interrupted, and propagates so
    pytest exits with its usual interrupted status.
    """
    if os.environ.get(SUBPROC_ENV) == "1":
        return None  # child runs the normal loop

    if session.config.option.collectonly:
        # Stock pytest's collect-only short-circuit, required because this
        # hook replaces the built-in loop
        # (https://github.com/dyollb/pytest-isolated/issues/53). Returning
        # True claims the firstresult hook and skips all execution; without
        # it, collection would launch children (--collect-only is Category B,
        # never forwarded, so the child would run the tests for real).
        return True

    config = session.config
    groups = getattr(config, CONFIG_ATTR_GROUPS, OrderedDict())
    group_timeouts: dict[str, int | None] = getattr(
        config, CONFIG_ATTR_GROUP_TIMEOUTS, {}
    )

    # session.items contains the final filtered and ordered
    # list (after -k, -m, --ff, etc.)
    # We need to:
    # 1. Filter groups to only include items in session.items
    # 2. Preserve the order from session.items (important for --ff, --nf, ...)

    # Build a mapping from nodeid to (item, group_name) for isolated tests
    nodeid_to_group: dict[str, tuple[pytest.Item, str]] = {}
    for group_name, group_items in groups.items():
        for it in group_items:
            nodeid_to_group[it.nodeid] = (it, group_name)

    # Rebuild groups in session.items order
    filtered_groups: OrderedDict[str, list[pytest.Item]] = OrderedDict()
    isolated_nodeids: set[str] = set()

    for it in session.items:
        if it.nodeid in nodeid_to_group:
            _, group_name = nodeid_to_group[it.nodeid]
            if group_name not in filtered_groups:
                filtered_groups[group_name] = []
            filtered_groups[group_name].append(it)
            isolated_nodeids.add(it.nodeid)

    groups = filtered_groups

    # If no isolated tests to run, return None to let default loop handle everything
    if not groups:
        return None

    # Normal items are those in session.items but not in isolated groups
    normal_items = [it for it in session.items if it.nodeid not in isolated_nodeids]

    # Get default timeout configuration
    timeout_opt = config.getoption("isolated_timeout", None)
    timeout_ini = config.getini("isolated_timeout")
    default_timeout = timeout_opt or (
        int(timeout_ini) if timeout_ini else DEFAULT_TIMEOUT
    )

    # Create execution context
    ctx = ExecutionContext(session=session)

    # Parallelism knob (spec: https://github.com/dyollb/pytest-isolated/issues/70).
    # Unset means serial: one Child at a time with the pre-parallel Launch plan.
    raw_n = config.getoption("isolated_numprocesses", None) or config.getini(
        "isolated_numprocesses"
    )
    max_parallel = resolve_numprocesses(raw_n or None)

    if max_parallel > 1 and config.getoption("capture", "fd") == "no":
        config.issue_config_time_warning(
            UserWarning(_PARALLEL_NO_CAPTURE_WARNING), stacklevel=1
        )
    # With Parent capture disabled, Children inherit the terminal (live,
    # interleaved) instead of being piped -- plain pytest -s semantics.
    capture_output = config.getoption("capture", "fd") != "no"

    serial_groups = frozenset(getattr(config, CONFIG_ATTR_GROUP_SERIAL, ()))

    isolated_items = [it for group_items in groups.values() for it in group_items]

    # The Parent owns the session fixture stack; Children keep their own
    # (a separate process cannot share fixture values). A setup error aborts
    # the whole run before any Child launches or any normal Item runs, and
    # teardown of partial setup rides pytest's session-end drain like any
    # other stock error path
    # (https://github.com/dyollb/pytest-isolated/issues/77).
    # On pytest 7 the mechanism is unavailable (fixture internals differ);
    # those runs keep the pre-#75 behavior, loudly warned.
    parent_fixtures_ok = _pytest_supports_parent_fixtures(pytest.__version__)

    setup_error: pytest.ExceptionInfo[BaseException] | None = None
    if parent_fixtures_ok:
        try:
            _setup_parent_session_fixtures(session, isolated_items)
        except (KeyboardInterrupt, Exit):
            raise
        except BaseException:
            setup_error = pytest.ExceptionInfo.from_current()
    else:
        config.issue_config_time_warning(
            UserWarning(
                _PARENT_FIXTURES_PYTEST_VERSION_WARNING.format(
                    version=pytest.__version__
                )
            ),
            stacklevel=1,
        )

    if setup_error is not None:
        _report_parent_fixture_outcome(
            session, isolated_items, setup_error, when="setup"
        )
        return 1

    if parent_fixtures_ok:
        _check_per_child_session_fixtures(session, isolated_items)

    tripped = _run_group_pool(
        session=session,
        config=config,
        ctx=ctx,
        groups=groups,
        group_timeouts=group_timeouts,
        default_timeout=default_timeout,
        serial_groups=serial_groups,
        max_parallel=max_parallel,
        capture_output=capture_output,
    )

    if not tripped:
        # Run normal tests in-process, after the pool has fully drained.
        for idx, item in enumerate(normal_items):
            nextitem = normal_items[idx + 1] if idx + 1 < len(normal_items) else None
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)

    # Drain the Parent fixture stack on every path that reaches loop end,
    # including maxfail/exitfirst: unlaunched Groups and in-process Items
    # stay silent (xdist shutdown semantics), but Parent finalizers must
    # still land before pytest_sessionfinish hooks (unlike the setup-error
    # abort above, which defers teardown to pytest's own session-end drain
    # per https://github.com/dyollb/pytest-isolated/issues/77).
    if parent_fixtures_ok:
        _teardown_parent_session_fixtures(session, isolated_items)

    return 1 if tripped or session.testsfailed else 0


def _process_group_result(
    *,
    config: pytest.Config,
    ctx: ExecutionContext,
    group_name: str,
    group_items: list[pytest.Item],
    spec: LaunchSpec,
    result: SubprocessResult,
    group_timeout: int,
    execution_time: float,
) -> None:
    """Classify one finished Child and emit its Group's outcome.

    Shared by the serial and parallel modes: setup-show re-emission, Report
    channel parsing, the crash/timeout handler chain, and normal emission.
    Always runs on the Parent's main thread, so each Group's output burst is
    atomic with respect to other Groups' bursts (completion-order emission).
    """
    # --setup-show prints fixture lifecycle to terminal output in child.
    # Re-emit those lines so users can see setup/teardown info for isolated
    # tests; in parallel mode this lands inside the Group's completion burst.
    if config.getoption("setupshow", False) and result.stdout:
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        setup_lines = [
            line
            for line in stdout_text.splitlines()
            if "SETUP" in line or "TEARDOWN" in line
        ]
        if setup_lines:
            sys.stdout.write("\n".join(setup_lines) + "\n")

    # Parse results
    results = _parse_results(spec.report_path)

    # Handle various failure conditions
    if _handle_xfail_crash(result.returncode, results, group_items, ctx):
        return

    if _handle_timeout(
        timed_out=result.timed_out,
        group_name=group_name,
        group_timeout=group_timeout,
        execution_time=execution_time,
        group_items=group_items,
        ctx=ctx,
        stdout=result.stdout,
        stderr=result.stderr,
    ):
        return

    if _handle_collection_crash(
        returncode=result.returncode,
        results=results,
        group_name=group_name,
        proc_stderr=result.stderr,
        group_items=group_items,
        ctx=ctx,
    ):
        return

    _handle_mid_test_crash(
        result.returncode, result.stderr, group_items, results, ctx
    )  # handled entries are popped; remaining results still get emitted

    # Emit normal test results
    _emit_all_results(group_items, results, ctx)


def _emit_interrupted(
    ctx: ExecutionContext,
    group_name: str,
    group_items: list[pytest.Item],
    spec: LaunchSpec,
    result: SubprocessResult,
) -> None:
    """Report a killed Child's Group as Interrupted, never as a crash.

    Completed phase records are emitted as-is; tests without a finished
    teardown are failed with an interruption message carrying the partial
    output (Partial-output invariant). Groups never launched stay silent --
    pytest-xdist's shutdown semantics.
    """
    msg = (
        f"Subprocess group={group_name!r} was interrupted "
        f"(KeyboardInterrupt) before it completed."
    )
    stdout_text = result.stdout.decode("utf-8", errors="replace")
    stderr_text = result.stderr.decode("utf-8", errors="replace")
    results = _parse_results(spec.report_path)
    unfinished = [
        it for it in group_items if "teardown" not in results.get(it.nodeid, {})
    ]
    for it in unfinished:
        results.pop(it.nodeid, None)
    if unfinished:
        _emit_failure_for_items(unfinished, msg, ctx.session, stdout_text, stderr_text)
    _emit_all_results(group_items, results, ctx)


def _maxfail_tripped(session: pytest.Session) -> bool:
    return bool(
        session.testsfailed
        and session.config.option.maxfail
        and session.testsfailed >= session.config.option.maxfail
    )


def _run_group_pool(
    *,
    session: pytest.Session,
    config: pytest.Config,
    ctx: ExecutionContext,
    groups: OrderedDict[str, list[pytest.Item]],
    group_timeouts: dict[str, int | None],
    default_timeout: int,
    serial_groups: frozenset[str],
    max_parallel: int,
    capture_output: bool = True,
) -> bool:
    """Run every Group through the Child Pool; True if maxfail tripped.

    One loop serves both modes: ``max_parallel == 1`` degenerates to the
    historical serial order (one Child alive at a time). Explicit basetemps
    get per-Group directories even in serial runs to protect Parent resources.
    Ctrl-C kills all live Children, reports their Groups as
    Interrupted, and re-raises for pytest's normal interrupted-exit path.
    """
    pending = list(groups)
    running: OrderedDict[str, RunningChild] = OrderedDict()
    # Exited Children still waiting for their pipes to drain before emission:
    # a grandchild that inherited the Child's pipes can keep a reader blocked
    # indefinitely, and joining it inside the sweep would freeze every
    # sibling's deadline enforcement. Each entry is the Child plus the
    # monotonic time it started finalizing.
    finalizing: OrderedDict[str, tuple[RunningChild, float]] = OrderedDict()
    slot = 0
    tripped = False
    temp_root: GroupTempRoot | None = None

    def _collect_and_emit(child: RunningChild, drain_timeout: float) -> None:
        nonlocal tripped
        result = child.finish(drain_timeout=drain_timeout)
        _process_group_result(
            config=config,
            ctx=ctx,
            group_name=child.name,
            group_items=groups[child.name],
            spec=child.spec,
            result=result,
            group_timeout=(group_timeouts.get(child.name) or default_timeout),
            execution_time=time.monotonic() - child.started,
        )
        if _maxfail_tripped(session):
            tripped = True

    if max_parallel > 1 or config.getoption("basetemp", None) is not None:
        temp_root = open_group_temp_root(config)
    try:
        while pending or running or finalizing:
            for name in decide_admissions(
                pending=pending,
                running_count=len(running),
                serial=serial_groups,
                max_parallel=max_parallel,
                maxfail_tripped=tripped,
                exclusive_running=any(
                    child.name in serial_groups for child in running.values()
                ),
            ):
                pending.remove(name)
                nodeids = [it.nodeid for it in groups[name]]
                group_timeout = group_timeouts.get(name) or default_timeout
                if temp_root is not None:
                    # The Launch plan owns directory naming; only concurrent
                    # Children need separate caches.
                    spec = plan_launch(
                        config,
                        nodeids,
                        hardened=Hardened(
                            root=temp_root.root,
                            slot=slot,
                            isolate_cache=max_parallel > 1,
                        ),
                    )
                    slot += 1
                else:
                    spec = plan_launch(config, nodeids)
                running[name] = launch_child(
                    name, spec, timeout=group_timeout, capture=capture_output
                )

            # Advance pending finalizations before sweeping so emission
            # keeps completion order (an older completion reports first).
            # Past the drain budget the Group reports with the output the
            # readers already hold: one Child's stuck pipes never hold the
            # loop -- or a sibling's deadline -- hostage.
            now = time.monotonic()
            for name in list(finalizing):
                finishing, entered = finalizing[name]
                if finishing.drain_done() or now - entered >= drain_join_timeout():
                    del finalizing[name]
                    _collect_and_emit(finishing, drain_timeout=0.0)

            # Sweep: snapshot the Children's plain facts, ask the pure
            # policy what each one gets, apply it. Reaps report on this
            # thread, in running order (completion-order emission).
            for action in decide_sweeps(
                [child.sweep_state() for child in running.values()],
                time.monotonic(),
            ):
                child = running[action.name]
                if isinstance(action, TerminateForTimeout):
                    # The applied action carries the timeout verdict. The
                    # request is non-blocking: a sibling ignoring terminate()
                    # cannot stall this sweep past other Children's deadlines.
                    # Spec: https://github.com/dyollb/pytest-isolated/issues/72
                    child.timed_out = True
                    child.request_kill()
                elif isinstance(action, EscalateToKill):
                    child.escalate_kill()
                else:
                    assert isinstance(action, Reap)
                    del running[action.name]
                    if child.drain_done():
                        # Healthy path: the pipes hit EOF the moment the
                        # Child exited, so its output is already complete.
                        _collect_and_emit(child, drain_timeout=0.0)
                    else:
                        # A descendant holds the pipes open: finalize off the
                        # sweep path instead of joining here.
                        finalizing[child.name] = (child, time.monotonic())

            if tripped and not running and not finalizing:
                # Drain complete (xdist exitfirst semantics): pending Groups
                # deliberately never launch; without this the loop would spin
                # with nothing left to admit or reap.
                break

            if pending or running or finalizing:
                time.sleep(poll_interval())
    except KeyboardInterrupt:
        for child in running.values():
            child.kill()
        for child in running.values():
            _emit_interrupted(
                ctx, child.name, groups[child.name], child.spec, child.finish()
            )
        # Children that completed before the interrupt keep their verdict:
        # only the pipe drain was outstanding, and partial output beats
        # rewriting a finished Group's outcome as Interrupted.
        for finishing, _entered in finalizing.values():
            _collect_and_emit(finishing, drain_timeout=0.0)
        finalizing.clear()
        running.clear()
        raise
    except BaseException:
        # Any other failure (a launch OSError, an emit or parsing bug) must
        # not leave Children running, nor let the finally block below delete
        # their directories while they still write to them. Kill and reap
        # every Child, then let the original error propagate -- the error is
        # the report, so nothing is emitted here. Per-Child suppression so
        # one unhealthy Child cannot mask the original exception or skip the
        # rest.
        for child in running.values():
            with contextlib.suppress(Exception):
                child.kill()
        for child in running.values():
            with contextlib.suppress(Exception):
                child.finish()
        for finishing, _entered in finalizing.values():
            with contextlib.suppress(Exception):
                finishing.finish(drain_timeout=0.0)
        finalizing.clear()
        running.clear()
        raise
    finally:
        if temp_root is not None and temp_root.temp_dir is not None:
            temp_root.temp_dir.cleanup()
    return tripped
