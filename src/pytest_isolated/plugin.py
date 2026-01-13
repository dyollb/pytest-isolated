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
from typing import Any

import pytest

# Guard to prevent infinite recursion (parent spawns child; child must not spawn again)
SUBPROC_ENV = "PYTEST_RUNNING_IN_SUBPROCESS"

# Parent tells child where to write JSONL records per test call
SUBPROC_REPORT_PATH = "PYTEST_SUBPROCESS_REPORT_PATH"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add configuration options for subprocess isolation."""
    group = parser.getgroup("isolated")
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
    """
    In the child process, write one JSON line per test phase (setup/call/teardown)
    containing outcome, captured stdout/stderr, duration, and other metadata.
    The parent will aggregate and re-emit this info.
    """
    path = os.environ.get(SUBPROC_REPORT_PATH)
    if not path:
        return

    # Capture ALL phases (setup, call, teardown), not just call
    rec = {
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
    """
    Partition items into subprocess groups + normal items and stash on config.
    """
    if os.environ.get(SUBPROC_ENV) == "1":
        return  # child should not do grouping

    # If --no-isolation is set, treat all tests as normal (no subprocess isolation)
    if config.getoption("no_isolation", False):
        config._subprocess_groups = OrderedDict()  # type: ignore[attr-defined]
        config._subprocess_normal_items = items  # type: ignore[attr-defined]
        return

    groups: OrderedDict[str, list[pytest.Item]] = OrderedDict()
    group_timeouts: dict[str, int | None] = {}  # Track timeout per group
    normal: list[pytest.Item] = []

    for item in items:
        m = item.get_closest_marker("isolated")
        if not m:
            normal.append(item)
            continue

        group = m.kwargs.get("group")
        # Default grouping to module path (so you don't accidentally group everything)
        if group is None:
            group = item.nodeid.split("::")[0]

        # Store group-specific timeout (first marker wins)
        group_key = str(group)
        if group_key not in group_timeouts:
            group_timeouts[group_key] = m.kwargs.get("timeout")

        groups.setdefault(group_key, []).append(item)

    config._subprocess_groups = groups  # type: ignore[attr-defined]
    config._subprocess_group_timeouts = group_timeouts  # type: ignore[attr-defined]
    config._subprocess_normal_items = normal  # type: ignore[attr-defined]


def pytest_runtestloop(session: pytest.Session) -> int | None:
    """
    Run each subprocess group in its own subprocess once;
    then run normal tests in-process.

    Enhanced to:
    - Capture all test phases (setup, call, teardown)
    - Support configurable timeouts
    - Properly handle crashes and missing results
    - Integrate with pytest's reporting system
    """
    if os.environ.get(SUBPROC_ENV) == "1":
        return None  # child runs the normal loop

    config = session.config
    groups: OrderedDict[str, list[pytest.Item]] = getattr(
        config, "_subprocess_groups", OrderedDict()
    )
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

    def emit_report(
        item: pytest.Item,
        when: str,
        outcome: str,
        longrepr: str = "",
        duration: float = 0.0,
        stdout: str = "",
        stderr: str = "",
        sections: list[tuple[str, str]] | None = None,
        user_properties: list[tuple[str, Any]] | None = None,
        wasxfail: bool = False,
    ) -> None:
        """
        Emit a synthetic report for the given item and phase.
        Attach captured output based on outcome and configuration.
        """
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
            rep.longrepr = (str(item.fspath), item.location[1], longrepr)
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

        # Run pytest in subprocess with timeout, tracking execution time
        cmd = [sys.executable, "-m", "pytest", *nodeids]
        start_time = time.time()

        try:
            proc = subprocess.run(
                cmd, env=env, timeout=group_timeout, capture_output=False, check=False
            )
            returncode = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            returncode = -1
            timed_out = True

        execution_time = time.time() - start_time

        # Gather results from JSONL file
        results: dict[str, dict[str, Any]] = {}
        report_file = Path(report_path)
        if report_file.exists():
            with report_file.open(encoding="utf-8") as f:
                for line in f:
                    file_line = line.strip()
                    if not file_line:
                        continue
                    rec = json.loads(file_line)
                    nodeid = rec["nodeid"]
                    when = rec["when"]

                    if nodeid not in results:
                        results[nodeid] = {}
                    results[nodeid][when] = rec
            with contextlib.suppress(OSError):
                report_file.unlink()

        # Handle timeout or crash
        if timed_out:
            msg = (
                f"Subprocess group={group_name!r} timed out after {group_timeout} "
                f"seconds (execution time: {execution_time:.2f}s). "
                f"Increase timeout with --isolated-timeout, isolated_timeout ini, "
                f"or @pytest.mark.isolated(timeout=N)."
            )
            for it in group_items:
                emit_report(it, "call", "failed", longrepr=msg)
                session.testsfailed += 1
            continue

        if not results:
            msg = (
                f"Subprocess group={group_name!r} exited with code {returncode} "
                f"and produced no per-test report. The subprocess may have "
                f"crashed during collection."
            )
            for it in group_items:
                emit_report(it, "call", "failed", longrepr=msg)
                session.testsfailed += 1
            continue

        # Emit per-test results into parent (all phases)
        for it in group_items:
            node_results = results.get(it.nodeid, {})

            # Emit setup, call, teardown in order
            for when in ["setup", "call", "teardown"]:
                if when not in node_results:
                    # If missing a phase, synthesize a passing one
                    if when == "call" and not node_results:
                        # Test completely missing - mark as failed
                        emit_report(
                            it,
                            "call",
                            "failed",
                            longrepr=f"Missing result from subprocess for {it.nodeid}",
                        )
                        session.testsfailed += 1
                    continue

                rec = node_results[when]
                emit_report(
                    it,
                    when=when,
                    outcome=rec["outcome"],
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

    # Run normal tests in-process
    for idx, item in enumerate(normal_items):
        nextitem = normal_items[idx + 1] if idx + 1 < len(normal_items) else None
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)

    return 1 if session.testsfailed else 0
