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
from typing import Any, Final, Literal, TypedDict, cast

import pytest

# Guard to prevent infinite recursion (parent spawns child; child must not spawn again)
SUBPROC_ENV: Final = "PYTEST_RUNNING_IN_SUBPROCESS"

# Parent tells child where to write JSONL records per test call
SUBPROC_REPORT_PATH: Final = "PYTEST_SUBPROCESS_REPORT_PATH"

# Options that should be forwarded to subprocess (flags without values)
_FORWARD_FLAGS: Final = {
    "-v",
    "--verbose",
    "-q",
    "--quiet",
    "-s",  # disable output capturing
    "-l",
    "--showlocals",
    "--strict-markers",
    "--strict-config",
    "-x",  # exit on first failure
    "--exitfirst",
}

# Options that should be forwarded to subprocess (options with values)
_FORWARD_OPTIONS_WITH_VALUE: Final = {
    "--tb",  # traceback style
    "-r",  # show extra test summary info
    "--capture",  # capture method (fd/sys/no/tee-sys)
}


class _TestRecord(TypedDict, total=False):
    """Structure for test phase results from subprocess."""

    nodeid: str
    when: Literal["setup", "call", "teardown"]
    outcome: Literal["passed", "failed", "skipped"]
    longrepr: str
    duration: float
    stdout: str
    stderr: str
    keywords: list[str]
    sections: list[tuple[str, str]]
    user_properties: list[tuple[str, Any]]
    wasxfail: bool


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("isolated")
    group.addoption(
        "--isolated",
        action="store_true",
        default=False,
        help="Run all tests in isolated subprocesses",
    )
    group.addoption(
        "--isolated-timeout",
        type=int,
        default=None,
        help="Timeout in seconds for isolated test groups (default: 300)",
    )
    group.addoption(
        "--no-isolation",
        action="store_true",
        default=False,
        help="Disable subprocess isolation (for debugging)",
    )
    parser.addini(
        "isolated_timeout",
        type="string",
        default="300",
        help="Default timeout in seconds for isolated test groups",
    )
    parser.addini(
        "isolated_capture_passed",
        type="bool",
        default=False,
        help="Capture output for passed tests (default: False)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "isolated(group=None, timeout=None): run this test in a grouped "
        "fresh Python subprocess; tests with the same group run together in "
        "one subprocess. timeout (seconds) overrides global --isolated-timeout.",
    )


# ----------------------------
# CHILD MODE: record results + captured output per test phase
# ----------------------------
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Write test phase results to a JSONL file when running in subprocess mode."""
    path = os.environ.get(SUBPROC_REPORT_PATH)
    if not path:
        return

    # Capture ALL phases (setup, call, teardown), not just call
    rec: _TestRecord = {
        "nodeid": report.nodeid,
        "when": report.when,  # setup, call, or teardown
        "outcome": report.outcome,  # passed/failed/skipped
        "longrepr": str(report.longrepr) if report.longrepr else "",
        "duration": getattr(report, "duration", 0.0),
        "stdout": getattr(report, "capstdout", "") or "",
        "stderr": getattr(report, "capstderr", "") or "",
        # Preserve test metadata for proper reporting
        "keywords": list(report.keywords),
        "sections": getattr(report, "sections", []),  # captured logs, etc.
        "user_properties": getattr(report, "user_properties", []),
        "wasxfail": hasattr(report, "wasxfail"),
    }
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ----------------------------
# PARENT MODE: group marked tests
# ----------------------------
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get(SUBPROC_ENV) == "1":
        return  # child should not do grouping

    # If --no-isolation is set, treat all tests as normal (no subprocess isolation)
    if config.getoption("no_isolation", False):
        config._subprocess_groups = OrderedDict()  # type: ignore[attr-defined]
        config._subprocess_normal_items = items  # type: ignore[attr-defined]
        return

    # If --isolated is set, run all tests in isolation
    run_all_isolated = config.getoption("isolated", False)

    groups: OrderedDict[str, list[pytest.Item]] = OrderedDict()
    group_timeouts: dict[str, int | None] = {}  # Track timeout per group
    normal: list[pytest.Item] = []

    for item in items:
        m = item.get_closest_marker("isolated")

        # Skip non-isolated tests unless --isolated flag is set
        if not m and not run_all_isolated:
            normal.append(item)
            continue

        # Get group from marker (positional arg, keyword arg, or default)
        group = None
        if m:
            # Support @pytest.mark.isolated("groupname") - positional arg
            if m.args:
                group = m.args[0]
            # Support @pytest.mark.isolated(group="groupname") - keyword arg
            elif "group" in m.kwargs:
                group = m.kwargs["group"]

        # Default grouping logic
        if group is None:
            # If --isolated flag is used (no explicit marker), use unique nodeid
            if not m:
                group = item.nodeid
            # Check if marker was applied to a class or module
            elif isinstance(item, pytest.Function):
                if item.cls is not None and hasattr(item.cls, "pytestmark"):
                    class_markers = getattr(item.cls, "pytestmark", [])
                    if not isinstance(class_markers, list):
                        class_markers = [class_markers]
                    has_class_marker = any(
                        getattr(cm, "name", None) == "isolated" for cm in class_markers
                    )
                    if has_class_marker:
                        # Group by class name (module::class)
                        parts = item.nodeid.split("::")
                        group = "::".join(parts[:2]) if len(parts) >= 3 else item.nodeid
                    else:
                        group = item.nodeid
                # Check if marker was applied at module level (pytestmark)
                elif hasattr(item.module, "pytestmark"):
                    module_markers = getattr(item.module, "pytestmark", [])
                    if not isinstance(module_markers, list):
                        module_markers = [module_markers]
                    has_module_marker = any(
                        getattr(mm, "name", None) == "isolated" for mm in module_markers
                    )
                    if has_module_marker:
                        # Group by module name (first part of nodeid)
                        parts = item.nodeid.split("::")
                        group = parts[0]
                    else:
                        group = item.nodeid
                else:
                    # Explicit marker on function uses unique nodeid
                    group = item.nodeid
            else:
                # Non-Function items use unique nodeid
                group = item.nodeid
                group = item.nodeid

        # Store group-specific timeout (first marker wins)
        group_key = str(group)
        if group_key not in group_timeouts:
            timeout = m.kwargs.get("timeout") if m else None
            group_timeouts[group_key] = timeout

        groups.setdefault(group_key, []).append(item)

    config._subprocess_groups = groups  # type: ignore[attr-defined]
    config._subprocess_group_timeouts = group_timeouts  # type: ignore[attr-defined]
    config._subprocess_normal_items = normal  # type: ignore[attr-defined]


def _emit_report(
    item: pytest.Item,
    when: Literal["setup", "call", "teardown"],
    outcome: Literal["passed", "failed", "skipped"],
    longrepr: str = "",
    duration: float = 0.0,
    stdout: str = "",
    stderr: str = "",
    sections: list[tuple[str, str]] | None = None,
    user_properties: list[tuple[str, Any]] | None = None,
    wasxfail: bool = False,
    capture_passed: bool = False,
) -> None:
    """Emit a test report for a specific test phase."""
    call = pytest.CallInfo.from_call(lambda: None, when=when)
    rep = pytest.TestReport.from_item_and_call(item, call)
    rep.outcome = outcome
    rep.duration = duration

    if user_properties:
        rep.user_properties = user_properties

    if wasxfail:
        rep.wasxfail = "reason: xfail"

    # For skipped tests, longrepr needs to be a tuple (path, lineno, reason)
    if outcome == "skipped" and longrepr:
        # Parse longrepr or create simple tuple
        lineno = item.location[1] if item.location[1] is not None else -1
        rep.longrepr = (str(item.fspath), lineno, longrepr)  # type: ignore[assignment]
    elif outcome == "failed" and longrepr:
        rep.longrepr = longrepr

    # Add captured output as sections (capstdout/capstderr are read-only)
    if outcome == "failed" or (outcome == "passed" and capture_passed):
        all_sections = list(sections) if sections else []
        if stdout:
            all_sections.append(("Captured stdout call", stdout))
        if stderr:
            all_sections.append(("Captured stderr call", stderr))
        if all_sections:
            rep.sections = all_sections

    item.ihook.pytest_runtest_logreport(report=rep)


def _emit_failure_for_items(
    items: list[pytest.Item],
    error_message: str,
    session: pytest.Session,
    capture_passed: bool = False,
) -> None:
    """Emit synthetic failure reports when subprocess execution fails.

    When a subprocess crashes, times out, or fails during collection, we emit
    synthetic test phase reports to mark affected tests as failed. We report
    setup="passed" and teardown="passed" (even though these phases never ran)
    to ensure pytest categorizes the test as FAILED rather than ERROR. The actual
    failure is reported in the call phase with the error message.

    For xfail tests, call is reported as skipped with wasxfail=True to maintain
    proper xfail semantics.
    """
    for it in items:
        xfail_marker = it.get_closest_marker("xfail")
        _emit_report(it, "setup", "passed", capture_passed=capture_passed)
        if xfail_marker:
            _emit_report(
                it,
                "call",
                "skipped",
                longrepr=error_message,
                wasxfail=True,
                capture_passed=capture_passed,
            )
        else:
            _emit_report(
                it,
                "call",
                "failed",
                longrepr=error_message,
                capture_passed=capture_passed,
            )
            session.testsfailed += 1
        _emit_report(it, "teardown", "passed", capture_passed=capture_passed)


def pytest_runtestloop(session: pytest.Session) -> int | None:
    """Execute isolated test groups in subprocesses and remaining tests in-process.

    Any subprocess timeouts are caught and reported as test failures; the
    subprocess.TimeoutExpired exception is not propagated to the caller.
    """
    if os.environ.get(SUBPROC_ENV) == "1":
        return None  # child runs the normal loop

    config = session.config
    groups = getattr(config, "_subprocess_groups", OrderedDict())
    if not isinstance(groups, OrderedDict):
        groups = OrderedDict()
    group_timeouts: dict[str, int | None] = getattr(
        config, "_subprocess_group_timeouts", {}
    )
    normal_items: list[pytest.Item] = getattr(
        config, "_subprocess_normal_items", session.items
    )

    # Get default timeout configuration
    timeout_opt = config.getoption("isolated_timeout", None)
    timeout_ini = config.getini("isolated_timeout")
    default_timeout = timeout_opt or (int(timeout_ini) if timeout_ini else 300)

    # Get capture configuration
    capture_passed = config.getini("isolated_capture_passed")

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
            skip_next = False

            for arg in config.invocation_params.args:
                if skip_next:
                    skip_next = False
                    continue

                # Forward only explicitly allowed options
                if arg in _FORWARD_FLAGS:
                    forwarded_args.append(arg)
                elif arg in _FORWARD_OPTIONS_WITH_VALUE:
                    forwarded_args.append(arg)
                    skip_next = True  # Next arg is the value
                elif arg.startswith(
                    tuple(f"{opt}=" for opt in _FORWARD_OPTIONS_WITH_VALUE)
                ):
                    forwarded_args.append(arg)

                # Skip everything else (positional args, test paths, unknown options)

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

        try:
            proc = subprocess.run(
                cmd,
                env=env,
                timeout=group_timeout,
                capture_output=True,
                check=False,
                cwd=subprocess_cwd,
            )
            returncode = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            returncode = -1
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
                _emit_failure_for_items(group_items, msg, session, capture_passed)
                continue

        # Handle timeout
        if timed_out:
            msg = (
                f"Subprocess group={group_name!r} timed out after {group_timeout} "
                f"seconds (execution time: {execution_time:.2f}s). "
                f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
                f"or @pytest.mark.isolated(timeout=N)."
            )
            _emit_failure_for_items(group_items, msg, session, capture_passed)
            continue

        # Handle crash during collection (no results produced)
        if not results:
            msg = (
                f"Subprocess group={group_name!r} exited with code {returncode} "
                f"and produced no per-test report. The subprocess may have "
                f"crashed during collection."
            )
            _emit_failure_for_items(group_items, msg, session, capture_passed)
            continue

        # Emit per-test results into parent (all phases)
        for it in group_items:
            node_results = results.get(it.nodeid, {})

            # Emit setup, call, teardown in order
            for when in ["setup", "call", "teardown"]:  # type: ignore[assignment]
                if when not in node_results:
                    # If missing a phase, synthesize a passing one
                    if when == "call" and not node_results:
                        # Test completely missing from subprocess results
                        msg = f"Missing result from subprocess for {it.nodeid}"
                        _emit_failure_for_items([it], msg, session, capture_passed)
                    continue

                rec = node_results[when]
                _emit_report(
                    it,
                    when=when,  # type: ignore[arg-type]
                    outcome=rec["outcome"],
                    longrepr=rec.get("longrepr", ""),
                    duration=rec.get("duration", 0.0),
                    stdout=rec.get("stdout", ""),
                    stderr=rec.get("stderr", ""),
                    capture_passed=capture_passed,
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
