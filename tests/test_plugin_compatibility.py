"""Test compatibility with other pytest plugins like pytest-forked and pytest-xdist."""

from __future__ import annotations

import sys

import pytest
from pytest import Pytester


@pytest.mark.skipif(
    sys.platform == "win32", reason="pytest-forked not available on Windows"
)
def test_forked_marker_runs_in_fork(pytester: Pytester) -> None:
    """Test that @pytest.mark.forked runs in a forked process and captures output."""
    pytest.importorskip("pytest_forked")

    pytester.makepyfile(
        """
        import os
        import sys
        import pytest

        parent_pid = os.getpid()

        @pytest.mark.forked
        def test_runs_in_fork():
            child_pid = os.getpid()
            print(f"MARKER: Running in PID {child_pid}")
            print(f"MARKER: Parent was PID {parent_pid}", file=sys.stderr)
            # If running in fork, PIDs must differ
            assert child_pid != parent_pid, f"Test ran in same process"
            # Force failure to see captured output
            assert False, "Intentional failure to verify output capture"
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    # Verify output from forked process was captured and is shown on failure
    output = result.stdout.str()
    assert "MARKER: Running in PID" in output
    assert "MARKER: Parent was PID" in output
    assert "Intentional failure to verify output capture" in output


@pytest.mark.skipif(
    sys.platform == "win32", reason="pytest-forked not available on Windows"
)
def test_forked_flag_runs_unmarked_test_in_fork(pytester: Pytester) -> None:
    """Test that --forked flag runs even unmarked tests in a fork."""
    pytest.importorskip("pytest_forked")

    pytester.makepyfile(
        """
        import os

        parent_pid = os.getpid()

        def test_runs_in_fork_via_flag():
            child_pid = os.getpid()
            # If --forked flag works, PIDs must differ even without marker
            assert child_pid != parent_pid, (
                f"Test ran in same process: parent={parent_pid}, child={child_pid}"
            )
    """
    )

    result = pytester.runpytest("-v", "--forked")
    result.assert_outcomes(passed=1)


def test_timeout_flag_enforces_timeout_on_unmarked_test(pytester: Pytester) -> None:
    """Test that --timeout flag enforces timeout on tests without markers."""
    pytest.importorskip("pytest_timeout")

    pytester.makepyfile(
        """
        import time

        def test_times_out_via_flag():
            # Sleep longer than the timeout
            time.sleep(5)
    """
    )

    result = pytester.runpytest_subprocess("-v", "--timeout=0.5")
    # Test should timeout and be marked as failed
    if sys.platform != "win32":
        result.assert_outcomes(failed=1)
    assert "Timeout" in result.stdout.str() or "timeout" in result.stdout.str()


def test_timeout_marker_enforces_timeout_in_isolated_tests(pytester: Pytester) -> None:
    """Test that @pytest.mark.timeout works correctly in isolated tests.

    Verifies that pytest-timeout plugin's timeout mechanism works correctly
    inside isolated subprocesses, allowing finer-grained timeout control than
    the group-level --isolated-timeout.
    """
    pytest.importorskip("pytest_timeout")

    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated
        @pytest.mark.timeout(0.5)
        def test_isolated_timeout_at_half_second():
            # Should timeout after 0.5 seconds (pytest-timeout)
            time.sleep(2)
            assert True

        @pytest.mark.isolated(group="slow", timeout=10)
        @pytest.mark.timeout(0.5)
        def test_isolated_group_timeout_at_half_second():
            # pytest-timeout (0.5s) should trigger before isolated timeout (10s)
            time.sleep(2)
            assert True
    """
    )

    result = pytester.runpytest_subprocess("-v")
    stdout = result.stdout.str()

    # On Windows, pytest-timeout may kill the process before the summary line is printed
    if sys.platform != "win32":
        # On POSIX systems, verify exact outcomes
        result.assert_outcomes(failed=2)

    # Verify pytest-timeout is reporting the timeouts (works on all platforms)
    assert "timeout" in stdout.lower() or "timed out" in stdout.lower()
    # Verify both tests were collected
    assert "test_isolated_timeout_at_half_second" in stdout
    assert "test_isolated_group_timeout_at_half_second" in stdout


def test_timeout_flag_works_with_isolated_tests(pytester: Pytester) -> None:
    """Test that --timeout flag works with isolated tests.

    Demonstrates that both plugins can be used simultaneously to get:
    - Process isolation from pytest-isolated
    - Per-test timeout enforcement from pytest-timeout
    """
    pytest.importorskip("pytest_timeout")

    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated
        def test_isolated_times_out():
            time.sleep(5)
            assert True

        def test_normal_times_out():
            time.sleep(5)
            assert True
    """
    )

    result = pytester.runpytest_subprocess("-v", "--timeout=0.5")
    stdout = result.stdout.str()

    # On Windows, pytest-timeout may kill the process before the summary line is printed
    if sys.platform != "win32":
        # On POSIX systems, verify exact outcomes
        result.assert_outcomes(failed=2)

    # Verify pytest-timeout is reporting the timeouts (works on all platforms)
    assert "timeout" in stdout.lower() or "timed out" in stdout.lower()
    # Verify all tests were collected
    assert "test_isolated_times_out" in stdout
    assert "test_normal_times_out" in stdout
