"""Configure pytest for testing the plugin."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


class _SingletonApplication:
    """Mock singleton application for testing isolation.

    Implements proper singleton pattern - only initializes once per process.
    """

    _instance: _SingletonApplication | None = None
    _initialized: bool = False

    def __new__(cls) -> _SingletonApplication:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Only initialize once - singleton pattern
        if not _SingletonApplication._initialized:
            _SingletonApplication._initialized = True
            self.ApplicationName = "TestApplication"
            self.state: dict[str, str] = {}

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for testing purposes."""
        cls._instance = None
        cls._initialized = False


@pytest.fixture(scope="function")
def application() -> _SingletonApplication:
    """Provide a singleton application instance.

    This fixture demonstrates singleton behavior - within the same process,
    all tests will get the same instance. Across isolated processes,
    each process will have its own singleton.
    """
    return _SingletonApplication()
