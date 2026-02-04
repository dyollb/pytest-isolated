"""Test result reporting for pytest-isolated."""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

import pytest

from .config import SUBPROC_ENV, SUBPROC_REPORT_PATH
from .utils import _has_isolated_marker, _TestRecord


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
        return

    # If --isolated is set, run all tests in isolation
    run_all_isolated = config.getoption("isolated", False)

    groups: OrderedDict[str, list[pytest.Item]] = OrderedDict()
    group_timeouts: dict[str, int | None] = {}  # Track timeout per group

    for item in items:
        m = item.get_closest_marker("isolated")

        # Skip non-isolated tests unless --isolated flag is set
        if not m and not run_all_isolated:
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
                if item.cls is not None and _has_isolated_marker(item.cls):
                    # Group by class name (module::class)
                    parts = item.nodeid.split("::")
                    group = "::".join(parts[:2]) if len(parts) >= 3 else item.nodeid
                elif _has_isolated_marker(item.module):
                    # Group by module name (first part of nodeid)
                    parts = item.nodeid.split("::")
                    group = parts[0]
                else:
                    # Explicit marker on function uses unique nodeid
                    group = item.nodeid
            else:
                # Non-Function items use unique nodeid
                group = item.nodeid

        # Store group-specific timeout (first marker wins)
        group_key = str(group)
        if group_key not in group_timeouts:
            timeout = m.kwargs.get("timeout") if m else None
            group_timeouts[group_key] = timeout

        groups.setdefault(group_key, []).append(item)

    config._subprocess_groups = groups  # type: ignore[attr-defined]
    config._subprocess_group_timeouts = group_timeouts  # type: ignore[attr-defined]


def _emit_report(
    item: pytest.Item,
    *,
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
        _emit_report(it, when="setup", outcome="passed", capture_passed=capture_passed)
        if xfail_marker:
            _emit_report(
                it,
                when="call",
                outcome="skipped",
                longrepr=error_message,
                wasxfail=True,
                capture_passed=capture_passed,
            )
        else:
            _emit_report(
                it,
                when="call",
                outcome="failed",
                longrepr=error_message,
                capture_passed=capture_passed,
            )
            session.testsfailed += 1
        _emit_report(
            it, when="teardown", outcome="passed", capture_passed=capture_passed
        )
