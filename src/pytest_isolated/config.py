"""Configuration and CLI options for pytest-isolated."""

from __future__ import annotations

import contextlib
import os
import shlex
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

# Incompatible pytest options (cannot be forwarded to subprocess).
# Forward all other options by default (blacklist approach).
_INCOMPATIBLE_OPTIONS: Final = {
    # Interactive/Debugger — require terminal input, unavailable in subprocess
    "--pdb": "requires interactive debugger access",
    "--pdbcls": "requires interactive debugger access",
    "--trace": "requires interactive debugger access",
    "--full-trace": "tied to interactive exception display",
    # Import/Discovery — affects module resolution differently in parent vs child
    "--confcutdir": "conftest search path differs between parent and child",
    "--noconftest": "parent and child need conftest for consistency",
    "--collect-in-virtualenv": "virtualenv detection may differ per process",
    # Caching/State — options that depend on child state remain incompatible
    "--nf": "file modification times differ between parent and child",
    "--new-first": "file modification times differ between parent and child",
    "--sw": "stepwise state tracking lost across subprocess boundary",
    "--stepwise": "stepwise state tracking lost across subprocess boundary",
    "--sw-skip": "stepwise state tracking lost across subprocess boundary",
    "--sw-reset": "stepwise state tracking lost across subprocess boundary",
    "--cache-show": "shows only parent's cache, not subprocess cache",
    "--cache-clear": "clears parent cache only, subprocess cache unaffected",
    # Configuration — config file selection must stay parent/child aligned
    "-c": "parent and child must use same config file",
    "--config-file": "parent and child must use same config file",
    # Fixture/Execution — prevent test execution or affect fixture scope
    "--setup-only": "setup-only prevents test execution in child",
    "--setup-plan": "setup-plan prevents test execution in child",
    # Debug pytest — incomplete debug info from parent only
    "--trace-config": "traces parent's conftest, not subprocess's",
    "--debug": "subprocess debug output may be lost",
    # xfail handling
    "--runxfail": "xfail handling inconsistent between parent and child",
}


def _validate_isolation_compatibility(config: pytest.Config) -> None:
    """Check for incompatible options when isolation is requested.

    Raises UsageError if incompatible option is explicitly passed.
    Also checks PYTEST_ADDOPTS environment variable.
    """
    # Get all args passed on CLI
    cli_args = config.invocation_params.args

    # Collect all args including from PYTEST_ADDOPTS env var
    all_args = list(cli_args)
    pytest_addopts = os.environ.get("PYTEST_ADDOPTS", "")
    if pytest_addopts:
        # Invalid shell syntax; let pytest handle it
        with contextlib.suppress(ValueError):
            all_args.extend(shlex.split(pytest_addopts))

    # Check for incompatible options
    for arg in all_args:
        # Handle --option=value format
        option = arg.split("=")[0] if "=" in arg else arg

        if option in _INCOMPATIBLE_OPTIONS:
            reason = _INCOMPATIBLE_OPTIONS[option]
            msg = (
                f"Option '{option}' is incompatible with @pytest.mark.isolated: "
                f"{reason}. Use --no-isolation to disable isolation for this run, "
                f"or remove the option."
            )
            raise pytest.UsageError(msg)


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
