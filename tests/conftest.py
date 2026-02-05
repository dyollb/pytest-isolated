"""Configure pytest for testing the plugin."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "timeout_plugin_required: test requires pytest-timeout plugin to be installed",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests that require pytest-timeout if it's not installed."""
    try:
        import pytest_timeout  # noqa

        timeout_available = True
    except ImportError:
        timeout_available = False

    if not timeout_available:
        skip_timeout = pytest.mark.skip(reason="pytest-timeout not installed")
        for item in items:
            if "timeout_plugin_required" in item.keywords:
                item.add_marker(skip_timeout)


class _SingletonApplication:
    """Mock singleton application for testing isolation.

    Implements proper singleton pattern - raises error on re-initialization
    to match real-world singleton behavior.
    """

    _instance: _SingletonApplication | None = None
    _init_called: bool

    def __new__(cls) -> _SingletonApplication:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_called = False
        return cls._instance

    def __init__(self) -> None:
        # Raise error if already initialized - mimics real singleton behavior
        if self._init_called:
            msg = "SingletonApplication already initialized"
            raise RuntimeError(msg)
        self._init_called = True
        self.ApplicationName = "TestApplication"
        self.state: dict[str, str] = {}

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for testing purposes."""
        cls._instance = None


@pytest.fixture(scope="session")
def application() -> _SingletonApplication:
    """Provide a singleton application instance.

    Uses session scope to ensure the fixture is only called once per subprocess.
    This matches the singleton pattern where initialization should only happen once.
    Within the same isolated process/group, all tests share the same instance.
    Across different isolated processes, each gets its own singleton.
    """
    return _SingletonApplication()
