"""Example tests demonstrating pytest-isolated usage."""

import pytest

# Global state to demonstrate isolation
_global_counter = 0


@pytest.mark.isolated(group="group1")
def test_subprocess_1():
    """This test runs in a subprocess with group1."""
    global _global_counter
    _global_counter += 1
    assert _global_counter == 1


@pytest.mark.isolated(group="group1")
def test_subprocess_2():
    """This test runs in the same subprocess as test_subprocess_1."""
    global _global_counter
    _global_counter += 1
    assert _global_counter == 2  # Sees state from test_subprocess_1


@pytest.mark.isolated(group="group2")
def test_subprocess_3():
    """This test runs in a different subprocess (fresh state)."""
    global _global_counter
    _global_counter += 1
    assert _global_counter == 1  # Fresh subprocess, fresh state


def test_normal():
    """This test runs in the main process."""
    global _global_counter
    _global_counter += 1
    # This will pass because subprocess tests don't affect main process
    assert _global_counter == 1
