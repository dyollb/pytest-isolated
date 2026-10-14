"""Subprocess execution for pytest-isolated."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal, NamedTuple, TypeAlias, cast

import pytest

from .config import (
    CONFIG_ATTR_GROUP_TIMEOUTS,
    CONFIG_ATTR_GROUPS,
    DEFAULT_TIMEOUT,
    SUBPROC_ENV,
)
from .launch import LaunchSpec, plan_launch
from .outcome import (
    Clean,
    CollectionCrash,
    Member,
    Outcome,
    PartialCrash,
    TimedOut,
    XfailCrash,
    classify,
)
from .reporting import (
    _TestRecord,
    emit_failure_for_items,
    emit_report,
    format_crash_message,
    get_xfail_reason,
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


def _run_subprocess(
    spec: LaunchSpec,
    *,
    timeout: int,
) -> SubprocessResult:
    """Run the planned child subprocess and return its result."""
    try:
        proc = subprocess.run(
            spec.cmd,
            env=spec.env,
            timeout=timeout,
            capture_output=True,
            check=False,
            cwd=spec.cwd,
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


def _emit_outcome(
    *,
    outcome: Outcome,
    result: SubprocessResult,
    results: TestResults,
    members: list[Member],
    group_name: str,
    group_timeout: int,
    execution_time: float,
    group_items: list[pytest.Item],
    ctx: ExecutionContext,
) -> None:
    """Emit synthetic reports for classified failures and ordinary child records."""
    items_by_nodeid = {item.nodeid: item for item in group_items}

    if isinstance(outcome, XfailCrash):
        msg = (
            f"Subprocess crashed with signal {-outcome.returncode} "
            f"(expected for xfail test)"
        )
        emit_failure_for_items(group_items, msg, ctx.session)
        return

    if isinstance(outcome, TimedOut):
        msg = (
            f"Subprocess group={group_name!r} timed out after {group_timeout} "
            f"seconds (execution time: {execution_time:.2f}s). "
            f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
            f"or @pytest.mark.isolated(timeout=N)."
        )
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        emit_failure_for_items(group_items, msg, ctx.session, stdout_text, stderr_text)
        return

    if isinstance(outcome, CollectionCrash):
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        msg = (
            f"Subprocess group={group_name!r} exited with code "
            f"{outcome.returncode} and produced no per-test report. The "
            f"subprocess may have crashed during collection."
        )
        if stderr_text:
            msg += f"\n\nSubprocess stderr:\n{stderr_text}"
        emit_failure_for_items(group_items, msg, ctx.session)
        return

    remaining_results = results
    if isinstance(outcome, PartialCrash):
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        crash_msg = format_crash_message(
            outcome.returncode, "during test execution", stderr_text
        )
        xfail_reasons = {member.nodeid: member.xfail_reason for member in members}

        for nodeid in outcome.crashed:
            item = items_by_nodeid[nodeid]
            node_results = results.get(nodeid, {})
            if "setup" in node_results:
                record = node_results["setup"]
                emit_report(
                    item,
                    when="setup",
                    outcome=record["outcome"],
                    longrepr=record.get("longrepr", ""),
                    duration=record.get("duration", 0.0),
                )
            else:
                emit_report(item, when="setup", outcome="passed")

            xfail_reason = xfail_reasons[nodeid]
            if xfail_reason:
                emit_report(
                    item,
                    when="call",
                    outcome="skipped",
                    longrepr=crash_msg,
                    wasxfail=xfail_reason,
                )
            else:
                emit_report(item, when="call", outcome="failed", longrepr=crash_msg)
                ctx.session.testsfailed += 1

            emit_report(item, when="teardown", outcome="passed")

        if outcome.not_run:
            not_run_msg = format_crash_message(
                outcome.returncode, "during earlier test execution", stderr_text
            )
            not_run_msg = f"Test did not run - {not_run_msg}"
            not_run_items = [items_by_nodeid[nodeid] for nodeid in outcome.not_run]
            emit_failure_for_items(not_run_items, not_run_msg, ctx.session)

        handled_nodeids = set(outcome.crashed) | set(outcome.not_run)
        remaining_results = {
            nodeid: node_results
            for nodeid, node_results in results.items()
            if nodeid not in handled_nodeids
        }

    elif not isinstance(outcome, Clean):
        raise AssertionError(f"Unhandled outcome: {outcome!r}")

    _emit_all_results(group_items, remaining_results, ctx)


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

        # Emit setup, call, teardown in order
        for when in phases:
            if when not in node_results:
                continue

            rec = node_results[when]
            emit_report(
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
    """Execute isolated test groups in subprocesses and remaining tests in-process.

    Any subprocess timeouts are caught and reported as test failures; the
    subprocess.TimeoutExpired exception is not propagated to the caller.
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

    # Run groups
    for group_name, group_items in groups.items():
        nodeids = [it.nodeid for it in group_items]

        # Get timeout for this group (marker timeout > global timeout)
        group_timeout = group_timeouts.get(group_name) or default_timeout

        # Everything the child is told (argv, env, cwd, report file) is
        # planned by one deep interface; see pytest_isolated.launch.
        spec = plan_launch(config, nodeids)

        # Run subprocess
        start_time = time.time()
        result = _run_subprocess(spec, timeout=group_timeout)
        execution_time = time.time() - start_time

        # --setup-show prints fixture lifecycle to terminal output in child.
        # Re-emit those lines so users can see setup/teardown info for isolated tests.
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

        members = [Member(it.nodeid, get_xfail_reason(it)) for it in group_items]
        outcome = classify(result, results, members)
        _emit_outcome(
            outcome=outcome,
            result=result,
            results=results,
            members=members,
            group_name=group_name,
            group_timeout=group_timeout,
            execution_time=execution_time,
            group_items=group_items,
            ctx=ctx,
        )

        # Check if we should exit early due to maxfail/exitfirst
        if isinstance(outcome, (Clean, PartialCrash)) and (
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
