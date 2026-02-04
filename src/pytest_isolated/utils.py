"""Helper functions and type definitions for pytest-isolated."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


def _has_isolated_marker(obj: Any) -> bool:
    """Check if an object has the isolated marker in its pytestmark."""
    markers = getattr(obj, "pytestmark", [])
    if not isinstance(markers, list):
        markers = [markers]
    return any(getattr(m, "name", None) == "isolated" for m in markers)


# ---------------------------------------------------------------------------
# Cross-platform crash detection helpers
# ---------------------------------------------------------------------------


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
