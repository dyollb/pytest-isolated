"""Test grouping logic for pytest-isolated.

Marker Precedence Rules
-----------------------
Following pytest's "closest marker wins" convention
(``get_closest_marker`` returns function > class > module), the
grouping logic resolves overlapping ``@pytest.mark.isolated`` markers
as follows:

1. **Explicit ``group`` always wins.**
   If the *closest* marker carries a ``group`` parameter (positional or
   keyword), that group name is used regardless of scope.

2. **Function-level marker wins (closest scope).**
   A function decorated with ``@pytest.mark.isolated`` — even inside an
   already-isolated class or module — runs in its **own** subprocess
   (keyed by ``nodeid``).  This matches pytest's standard precedence:
   the closest marker takes effect.

3. **Class scope groups methods.**
   ``@pytest.mark.isolated`` on a class (without a function-level
   override) groups all its methods into one subprocess (keyed
   ``module::class``).

4. **Module scope groups functions.**
   ``pytestmark = pytest.mark.isolated`` groups all functions and
   un-decorated class methods in the module into one subprocess
   (keyed by module path).

5. **Timeout is a group-level concept.**
   The ``timeout`` parameter applies to the entire subprocess group.
   Use ``pytest-timeout`` for per-test timeouts within a group.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

import pytest

from .config import CONFIG_ATTR_GROUP_TIMEOUTS, CONFIG_ATTR_GROUPS, SUBPROC_ENV


def _has_isolated_marker(obj: Any) -> bool:
    """Check if an object has the isolated marker in its pytestmark."""
    markers = getattr(obj, "pytestmark", [])
    if not isinstance(markers, list):
        markers = [markers]
    return any(getattr(m, "name", None) == "isolated" for m in markers)


def _has_own_isolated_marker(item: pytest.Item) -> bool:
    """Check if item has isolated marker directly on it (not inherited)."""
    return any(m.name == "isolated" for m in item.own_markers)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get(SUBPROC_ENV) == "1":
        return  # child should not do grouping

    # If --no-isolation is set, treat all tests as normal (no subprocess isolation)
    if config.getoption("no_isolation", False):
        setattr(config, CONFIG_ATTR_GROUPS, OrderedDict())
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

        # --- Step 1: explicit group from closest marker wins ---
        group = None
        if m:
            # Support @pytest.mark.isolated("groupname") - positional arg
            if m.args:
                group = m.args[0]
            # Support @pytest.mark.isolated(group="groupname") - keyword arg
            elif "group" in m.kwargs:
                group = m.kwargs["group"]

        # --- Step 2: default grouping — closest scope wins ---
        if group is None:
            # If --isolated flag is used (no explicit marker), use unique nodeid
            if not m:
                group = item.nodeid
            elif isinstance(item, pytest.Function):
                # Closest wins: function-level marker takes priority
                if _has_own_isolated_marker(item):
                    # Function has its own @isolated → own subprocess
                    group = item.nodeid
                elif item.cls is not None and _has_isolated_marker(item.cls):
                    # Class scope: group by class (module::class)
                    parts = item.nodeid.split("::")
                    group = "::".join(parts[:2]) if len(parts) >= 3 else item.nodeid
                elif _has_isolated_marker(item.module):
                    # Module scope: group by module path
                    parts = item.nodeid.split("::")
                    group = parts[0]
                else:
                    # Marker on function only: own subprocess
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

    setattr(config, CONFIG_ATTR_GROUPS, groups)
    setattr(config, CONFIG_ATTR_GROUP_TIMEOUTS, group_timeouts)
