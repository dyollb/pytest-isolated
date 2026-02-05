"""pytest-isolated plugin - Run tests in isolated subprocesses.

This plugin allows running tests in isolated subprocesses to prevent state leakage.
"""

from __future__ import annotations

from .config import pytest_addoption, pytest_configure
from .execution import pytest_runtestloop
from .grouping import pytest_collection_modifyitems
from .reporting import pytest_runtest_logreport

__all__: tuple[str, ...] = (
    "pytest_addoption",
    "pytest_collection_modifyitems",
    "pytest_configure",
    "pytest_runtest_logreport",
    "pytest_runtestloop",
)
