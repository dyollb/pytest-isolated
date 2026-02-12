"""Subprocess execution for pytest-isolated."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal, NamedTuple, TypeAlias, cast

import pytest

from .config import (
    _FORWARD_FLAGS,
    _FORWARD_OPTIONS_WITH_VALUE,
    CONFIG_ATTR_GROUP_TIMEOUTS,
    CONFIG_ATTR_GROUPS,
    DEFAULT_TIMEOUT,
    SUBPROC_ENV,
    SUBPROC_REPORT_PATH,
)
from .reporting import (
    _emit_failure_for_items,
    _emit_report,
    _format_crash_message,
    _TestRecord,
)

# Type aliases for clarity
Phase: TypeAlias = Literal["setup", "call", "teardown"]
TestResults: TypeAlias = dict[str, dict[str, _TestRecord]]


class SubprocessResult(NamedTuple):
    """Result from running a subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class ExecutionContext(NamedTuple):
    """Context for test execution."""

    session: pytest.Session


def _build_forwarded_args(config: pytest.Config) -> list[str]:
    """Build list of pytest arguments to forward to subprocess."""
    forwarded_args: list[str] = []
    i = 0
    args = config.invocation_params.args
    while i < len(args):
        arg = args[i]

        # Forward only explicitly allowed options
        if arg in _FORWARD_FLAGS:
            forwarded_args.append(arg)
            i += 1
        elif arg in _FORWARD_OPTIONS_WITH_VALUE:
            forwarded_args.append(arg)
            # Next arg is the value - forward it too
            if i + 1 < len(args):
                forwarded_args.append(args[i + 1])
                i += 2
            else:
                i += 1
        elif arg.startswith(tuple(f"{opt}=" for opt in _FORWARD_OPTIONS_WITH_VALUE)):
            forwarded_args.append(arg)
            i += 1
        else:
            # Skip everything else (positional args, test paths,
            # unknown options)
            i += 1
    return forwarded_args


def _run_subprocess(
    cmd: list[str],
    env: dict[str, str],
    timeout: int,
    cwd: str | None,
) -> SubprocessResult:
    """Run subprocess and return result."""
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            timeout=timeout,
            capture_output=True,
            check=False,
            cwd=cwd,
        )
        return SubprocessResult(
            returncode=proc.returncode,
            stdout=proc.stdout or b"",
            stderr=proc.stderr or b"",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired exception contains partial output captured before timeout
        return SubprocessResult(
            returncode=-1,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
            timed_out=True,
        )


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
    timed_out: bool,
    group_name: str,
    group_timeout: int,
    execution_time: float,
    group_items: list[pytest.Item],
    ctx: ExecutionContext,
) -> bool:
    """Handle subprocess timeout. Returns True if handled."""
    if timed_out:
        msg = (
            f"Subprocess group={group_name!r} timed out after {group_timeout} "
            f"seconds (execution time: {execution_time:.2f}s). "
            f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
            f"or @pytest.mark.isolated(timeout=N)."
        )
        _emit_failure_for_items(group_items, msg, ctx.session)
        return True
    return False


def _handle_collection_crash(
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
            xfail_marker = it.get_closest_marker("xfail")
            if xfail_marker:
                _emit_report(
                    it,
                    when="call",
                    outcome="skipped",
                    longrepr=crash_msg,
                    wasxfail=True,
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
                outcome=rec.get("outcome", "failed"),  # type: ignore[arg-type]
                longrepr=rec.get("longrepr", ""),
                duration=rec.get("duration", 0.0),
                stdout=rec.get("stdout", ""),
                stderr=rec.get("stderr", ""),
                sections=rec.get("sections"),
                user_properties=rec.get("user_properties"),
                wasxfail=rec.get("wasxfail", False),
            )

            if when == "call" and rec["outcome"] == "failed":
                ctx.session.testsfailed += 1


def pytest_runtestloop(session: pytest.Session) -> int | None:
    """Execute isolated test groups in subprocesses and remaining tests in-process.

    Any subprocess timeouts are caught and reported as test failures; the
    subprocess.TimeoutExpired exception is not propagated to the caller.
    """
    if os.environ.get(SUBPROC_ENV) == "1":
        return None  # child runs the normal loop

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

    # Run groups
    for group_name, group_items in groups.items():
        nodeids = [it.nodeid for it in group_items]

        # Get timeout for this group (marker timeout > global timeout)
        group_timeout = group_timeouts.get(group_name) or default_timeout

        # file where the child will append JSONL records
        with tempfile.NamedTemporaryFile(
            prefix="pytest-subproc-", suffix=".jsonl", delete=False
        ) as tf:
            report_path = tf.name

        env = os.environ.copy()
        env[SUBPROC_ENV] = "1"
        env[SUBPROC_REPORT_PATH] = report_path

        # Python CLI
        #
        # -m mod : run library module as a script (terminates option list
        # -u     : force the stdout and stderr streams to be unbuffered;
        #         this option has no effect on stdin; also PYTHONUNBUFFERED=x
        #
        cmd = [
            sys.executable,
            "-u",  # Disable buffering so we capture partial output on timeout
            "-m",
            "pytest",
        ]

        # Build forwarded args and subprocess command
        forwarded_args = _build_forwarded_args(config)
        cmd.extend(forwarded_args)

        # Pass rootdir to subprocess to ensure it uses the same project root
        if config.rootpath:
            cmd.extend(["--rootdir", str(config.rootpath)])

        # Add the test nodeids
        cmd.extend(nodeids)

        # Determine the working directory for the subprocess
        # Use rootpath if set, otherwise use invocation directory
        # This ensures nodeids (which are relative to rootpath) can be resolved
        if config.rootpath:
            subprocess_cwd = str(config.rootpath)
        else:
            subprocess_cwd = str(config.invocation_params.dir)

        # Run subprocess
        start_time = time.time()
        result = _run_subprocess(cmd, env, group_timeout, subprocess_cwd)
        execution_time = time.time() - start_time

        # Forward subprocess stdout to parent (so prints are visible)
        if result.stdout:
            # NOTE: In some pytest environments (or when output is redirected),
            # sys.stdout may be a text-only stream (or a custom object)
            # without .buffer,

            stdout_buffer = getattr(sys.stdout, "buffer", None)
            if stdout_buffer is not None:
                stdout_buffer.write(result.stdout)
                stdout_buffer.flush()
            else:
                # Fallback for text-only or custom stdout streams without .buffer
                encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
                sys.stdout.write(result.stdout.decode(encoding, errors="replace"))
                sys.stdout.flush()

        # Parse results
        results = _parse_results(report_path)

        # Handle various failure conditions
        if _handle_xfail_crash(result.returncode, results, group_items, ctx):
            continue

        if _handle_timeout(
            result.timed_out,
            group_name,
            group_timeout,
            execution_time,
            group_items,
            ctx,
        ):
            continue

        if _handle_collection_crash(
            result.returncode, results, group_name, result.stderr, group_items, ctx
        ):
            continue

        if _handle_mid_test_crash(
            result.returncode, result.stderr, group_items, results, ctx
        ):
            pass  # Continue to emit remaining results

        # Emit normal test results
        _emit_all_results(group_items, results, ctx)

        # Check if we should exit early due to maxfail/exitfirst
        if (
            session.testsfailed
            and session.config.option.maxfail
            and session.testsfailed >= session.config.option.maxfail
        ):
            return 1

    # Run normal tests in-process
    for idx, item in enumerate(normal_items):
        nextitem = normal_items[idx + 1] if idx + 1 < len(normal_items) else None
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)

    return 1 if session.testsfailed else 0
