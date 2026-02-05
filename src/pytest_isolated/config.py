"""Configuration and CLI options for pytest-isolated."""

from __future__ import annotations

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
    "--capture",  # output capture method (fd, sys, no, tee-sys)
}


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
    parser.addini(
        "isolated_timeout",
        type="string",
        default=str(DEFAULT_TIMEOUT),
        help="Default timeout in seconds for isolated test groups",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "isolated(group=None, timeout=None): run this test in a grouped "
        "fresh Python subprocess; tests with the same group run together in "
        "one subprocess. timeout (seconds) overrides global --isolated-timeout.",
    )
