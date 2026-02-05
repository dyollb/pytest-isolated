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
from typing import cast

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

        # Forward relevant pytest options to subprocess for consistency
        # Only forward specific options that affect test execution behavior
        forwarded_args = []
        if hasattr(config, "invocation_params") and hasattr(
            config.invocation_params, "args"
        ):
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
                elif arg.startswith(
                    tuple(f"{opt}=" for opt in _FORWARD_OPTIONS_WITH_VALUE)
                ):
                    forwarded_args.append(arg)
                    i += 1
                else:
                    # Skip everything else (positional args, test paths,
                    # unknown options)
                    i += 1

        # Build pytest command for subprocess
        cmd = [sys.executable, "-m", "pytest"]
        cmd.extend(forwarded_args)

        # Pass rootdir to subprocess to ensure it uses the same project root
        if config.rootpath:
            cmd.extend(["--rootdir", str(config.rootpath)])

        # Add the test nodeids
        cmd.extend(nodeids)

        start_time = time.time()

        # Determine the working directory for the subprocess
        # Use rootpath if set, otherwise use invocation directory
        # This ensures nodeids (which are relative to rootpath) can be resolved
        subprocess_cwd = None
        if config.rootpath:
            subprocess_cwd = str(config.rootpath)
        elif hasattr(config, "invocation_params") and hasattr(
            config.invocation_params, "dir"
        ):
            subprocess_cwd = str(config.invocation_params.dir)

        # Check if capture is disabled (-s or --capture=no)
        # If so, don't capture subprocess output to allow it to flow to terminal
        capture_disabled = "-s" in forwarded_args or "--capture=no" in forwarded_args

        proc_stderr = b""
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                timeout=group_timeout,
                capture_output=not capture_disabled,
                check=False,
                cwd=subprocess_cwd,
            )
            returncode = proc.returncode
            proc_stderr = proc.stderr or b"" if not capture_disabled else b""
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = -1
            proc_stderr = exc.stderr or b"" if not capture_disabled else b""
            timed_out = True

        execution_time = time.time() - start_time

        # Gather results from JSONL file
        results: dict[str, dict[str, _TestRecord]] = {}
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

        # For crashes (negative returncode), check if we should treat as xfail
        if returncode < 0 and results:
            # Check if all tests in this group are marked xfail
            all_xfail = all(it.get_closest_marker("xfail") for it in group_items)
            if all_xfail:
                # Override any results from subprocess - crash is the expected outcome
                msg = (
                    f"Subprocess crashed with signal {-returncode} "
                    f"(expected for xfail test)"
                )
                _emit_failure_for_items(group_items, msg, session)
                continue

        # Handle timeout
        if timed_out:
            msg = (
                f"Subprocess group={group_name!r} timed out after {group_timeout} "
                f"seconds (execution time: {execution_time:.2f}s). "
                f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
                f"or @pytest.mark.isolated(timeout=N)."
            )
            _emit_failure_for_items(group_items, msg, session)
            continue

        # Handle crash during collection (no results produced)
        if not results:
            stderr_text = proc_stderr.decode("utf-8", errors="replace").strip()
            msg = (
                f"Subprocess group={group_name!r} exited with code {returncode} "
                f"and produced no per-test report. The subprocess may have "
                f"crashed during collection."
            )
            if stderr_text:
                msg += f"\n\nSubprocess stderr:\n{stderr_text}"
            _emit_failure_for_items(group_items, msg, session)
            continue

        # Handle mid-test crash: detect tests with incomplete phases
        # (e.g., setup recorded but call missing indicates crash during test)
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

        if crashed_items or not_run_items:
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
                        session.testsfailed += 1

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
                _emit_failure_for_items(not_run_items, not_run_msg, session)
                for it in not_run_items:
                    results.pop(it.nodeid, None)

        # Emit per-test results into parent (all phases)
        for it in group_items:
            node_results = results.get(it.nodeid, {})

            # Skip tests that were already handled by crash detection above
            if it.nodeid not in results:
                continue

            # Check if setup passed (to determine if missing call is expected)
            setup_passed = (
                "setup" in node_results and node_results["setup"]["outcome"] == "passed"
            )

            # Emit setup, call, teardown in order
            for when in ["setup", "call", "teardown"]:  # type: ignore[assignment]
                if when not in node_results:
                    # If missing call phase AND setup passed, emit a failure
                    # (crash detection above should handle most cases, but this
                    # is a safety net for unexpected situations)
                    # If setup failed, missing call is expected (pytest skips call)
                    if when == "call" and setup_passed:
                        msg = (
                            "Missing 'call' phase result"
                            f" from subprocess for {it.nodeid}"
                        )
                        _emit_report(
                            it,
                            when="call",
                            outcome="failed",
                            longrepr=msg,
                        )
                        session.testsfailed += 1
                    continue

                rec = node_results[when]
                _emit_report(
                    it,
                    when=when,  # type: ignore[arg-type]
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
                    session.testsfailed += 1

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
