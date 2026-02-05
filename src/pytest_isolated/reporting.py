"""Test result reporting for pytest-isolated."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, TypedDict

import pytest

from .config import SUBPROC_REPORT_PATH


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


def _format_crash_reason(returncode: int) -> str:
    """Format a human-readable crash reason from a return code.

    On Unix, negative return codes indicate signal numbers.
    On Windows, we report the exit code directly.
    """
    if returncode < 0:
        # Unix: negative return code is -signal_number
        return f"crashed with signal {-returncode}"
    # Windows or other: positive exit code
    return f"crashed with exit code {returncode}"


def _format_crash_message(
    returncode: int,
    context: str,
    stderr_text: str = "",
) -> str:
    """Build a complete crash error message with optional stderr output.

    Args:
        returncode: The subprocess return code.
        context: Description of when the crash occurred (e.g., "during test execution").
        stderr_text: Optional captured stderr from the subprocess.

    Returns:
        A formatted error message suitable for test failure reports.
    """
    reason = _format_crash_reason(returncode)
    msg = f"Subprocess {reason} {context}."
    if stderr_text:
        msg += f"\n\nSubprocess stderr:\n{stderr_text}"
    return msg


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

    # Attach captured output to the report when present.
    # Pytest's reporting layer will decide whether to display it based on
    # test outcome, verbosity, and capture settings (--capture, -s, etc.)
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
        _emit_report(it, when="setup", outcome="passed")
        if xfail_marker:
            _emit_report(
                it,
                when="call",
                outcome="skipped",
                longrepr=error_message,
                wasxfail=True,
            )
        else:
            _emit_report(
                it,
                when="call",
                outcome="failed",
                longrepr=error_message,
            )
            session.testsfailed += 1
        _emit_report(it, when="teardown", outcome="passed")
