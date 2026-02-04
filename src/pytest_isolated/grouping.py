"""Test grouping logic for pytest-isolated."""

from __future__ import annotations

import os
from collections import OrderedDict

import pytest

from .config import SUBPROC_ENV
from .utils import _has_isolated_marker


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
