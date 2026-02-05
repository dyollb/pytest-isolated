"""Integration tests with other pytest plugins.

Tests that pytest-isolated works correctly with other popular pytest plugins.
"""

import pytest

pytest_plugins = ["pytester"]


@pytest.mark.timeout_plugin_required
def test_timeout_plugin_integration_isolated(pytester: pytest.Pytester):
    """Test that pytest-timeout enforces per-test timeout in isolated tests.

    Verifies that pytest-timeout plugin's timeout mechanism works correctly
    inside isolated subprocesses, allowing finer-grained timeout control than
    the group-level --isolated-timeout.
    """
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
    result.assert_outcomes(failed=2)
    stdout = result.stdout.str()
    # Verify pytest-timeout is reporting the timeouts
    assert "timeout" in stdout.lower() or "timed out" in stdout.lower()


@pytest.mark.timeout_plugin_required
def test_timeout_plugin_integration_normal(pytester: pytest.Pytester):
    """Test that pytest-timeout works with normal (non-isolated) tests.

    Baseline test to verify pytest-timeout works in the test environment.
    """
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.timeout(0.5)
        def test_normal_with_timeout():
            # Should timeout after 0.5 seconds
            time.sleep(5)
            assert True
    """
    )

    result = pytester.runpytest_subprocess("-v")
    result.assert_outcomes(failed=1)
    stdout = result.stdout.str()
    assert "timeout" in stdout.lower() or "timed out" in stdout.lower()


@pytest.mark.timeout_plugin_required
def test_timeout_plugin_integration_mixed(pytester: pytest.Pytester):
    """Test that pytest-timeout and pytest-isolated can be composed together.

    Demonstrates that both plugins can be used simultaneously to get:
    - Process isolation from pytest-isolated
    - Per-test timeout enforcement from pytest-timeout
    """
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated
        @pytest.mark.timeout(0.5)
        def test_isolated_times_out():
            time.sleep(5)
            assert True

        @pytest.mark.timeout(0.5)
        def test_normal_times_out():
            time.sleep(5)
            assert True

        @pytest.mark.isolated(group="grouped")
        @pytest.mark.timeout(0.5)
        def test_grouped_1_times_out():
            time.sleep(5)
            assert True

        @pytest.mark.isolated(group="grouped")
        @pytest.mark.timeout(0.5)
        def test_grouped_2_times_out():
            time.sleep(5)
            assert True
    """
    )

    result = pytester.runpytest_subprocess("-v")
    # All tests should timeout
    result.assert_outcomes(failed=4)
    stdout = result.stdout.str()
    assert "timeout" in stdout.lower() or "timed out" in stdout.lower()
