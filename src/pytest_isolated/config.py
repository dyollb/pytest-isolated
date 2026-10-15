"""Configuration and CLI options for pytest-isolated."""

from __future__ import annotations

import os
from typing import Final

import pytest

# Guard to prevent infinite recursion (parent spawns child; child must not spawn again)
SUBPROC_ENV: Final = "PYTEST_RUNNING_IN_SUBPROCESS"

# Parent tells child where to write JSONL records per test call
SUBPROC_REPORT_PATH: Final = "PYTEST_SUBPROCESS_REPORT_PATH"

# Default timeout for isolated test groups (seconds)
DEFAULT_TIMEOUT: Final = 300

# Config attribute names (stored on pytest.Config object)
CONFIG_ATTR_GROUPS: Final = "_subprocess_groups"
CONFIG_ATTR_GROUP_TIMEOUTS: Final = "_subprocess_group_timeouts"
CONFIG_ATTR_GROUP_SERIAL: Final = "_subprocess_group_serial"


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
        help=(
            f"Timeout in seconds for isolated test groups (default: {DEFAULT_TIMEOUT})"
        ),
    )
    group.addoption(
        "--no-isolation",
        action="store_true",
        default=False,
        help="Disable subprocess isolation (for debugging)",
    )
    group.addoption(
        "--isolated-numprocesses",
        type=str,
        default=None,
        help=(
            "Run up to N isolated test groups concurrently in Child processes "
            "(integer or 'auto' for the CPU count of this process). "
            "Default unset: serial, one Child at a time. "
            "Analogous to pytest-xdist's -n/--numprocesses."
        ),
    )
    parser.addini(
        "isolated_timeout",
        type="string",
        default=str(DEFAULT_TIMEOUT),
        help="Default timeout in seconds for isolated test groups",
    )
    parser.addini(
        "isolated_numprocesses",
        type="string",
        default="",
        help=(
            "Default for --isolated-numprocesses: number of isolated test "
            "groups to run concurrently (integer or 'auto'). Empty: serial."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "isolated(group=None, timeout=None, serial=False): run this test in a "
        "grouped fresh Python subprocess; tests with the same group run "
        "together in one subprocess. timeout (seconds) overrides global "
        "--isolated-timeout. serial=True makes the whole Group Exclusive: it "
        "runs alone, never concurrently with other Children when "
        "--isolated-numprocesses is above 1.",
    )


def _auto_process_count() -> int:
    """Count of CPUs usable by THIS process (matches modern pytest-xdist).

    POSIX: the affinity mask when available (correct inside cpuset/container
    restrictions); Windows: os.process_cpu_count() when available (3.13+).
    Falls back to os.cpu_count(), then 1.
    """
    if hasattr(os, "sched_getaffinity"):  # POSIX only
        try:
            count = len(os.sched_getaffinity(0))
        except OSError:  # pragma: no cover - defensive
            count = 0
        if count > 0:
            return count
    else:  # pragma: no cover - Windows path, exercised on Windows CI
        process_cpu_count = getattr(os, "process_cpu_count", None)
        if process_cpu_count is not None:
            try:
                count = process_cpu_count()
            except NotImplementedError:  # pragma: no cover - defensive
                count = 0
            # process_cpu_count() may return None when undeterminable.
            if count is not None and count > 0:
                return count
    return os.cpu_count() or 1


def resolve_numprocesses(raw: str | None) -> int:
    """Resolve the numprocesses knob to a concurrency cap >= 1.

    ``None``/empty (the default) means serial; ``auto`` resolves affinity-
    aware; an integer is used as-is. Invalid values raise a UsageError naming
    the offending value (spec: https://github.com/dyollb/pytest-isolated/issues/70).
    """
    token = "" if raw is None else str(raw).strip()
    if not token:
        return 1
    if token.lower() == "auto":
        return _auto_process_count()
    try:
        value = int(token)
    except ValueError:
        msg = f"isolated_numprocesses: expected an integer or 'auto', got {raw!r}"
        raise pytest.UsageError(msg) from None
    if value < 1:
        msg = (
            f"isolated_numprocesses: expected a positive integer or 'auto', got {raw!r}"
        )
        raise pytest.UsageError(msg)
    return value
