"""Tests for pytest filtering and ordering options with isolated tests.

This module tests that pytest's built-in filtering options (-k, -m, --lf, --ff, --nf)
work correctly with isolated tests.
"""

from pytest import Pytester


def test_k_filtering_with_isolated_tests(pytester: Pytester):
    """Test that -k option works with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated(group="group1")
        def test_foo_one():
            assert True

        @pytest.mark.isolated(group="group1")
        def test_foo_two():
            assert True

        @pytest.mark.isolated(group="group2")
        def test_bar_one():
            assert True

        @pytest.mark.isolated(group="group2")
        def test_bar_two():
            assert True
    """
    )

    # Filter for tests with "foo" in the name
    result = pytester.runpytest("-v", "-k", "foo")
    result.assert_outcomes(passed=2)


def test_k_filtering_with_isolated_and_normal_tests(pytester: Pytester):
    """Test that -k option works with mixed isolated and normal tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_isolated_match_this():
            assert True

        @pytest.mark.isolated
        def test_isolated_skip_this():
            assert True

        def test_normal_match_this():
            assert True

        def test_normal_skip_this():
            assert True
    """
    )

    # Filter for tests with "match" in the name
    result = pytester.runpytest("-v", "-k", "match")
    result.assert_outcomes(passed=2)


def test_k_filtering_complex_expression(pytester: Pytester):
    """Test that -k option works with complex boolean expressions."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated(group="mygroup")
        def test_foo():
            assert True

        @pytest.mark.isolated(group="mygroup")
        def test_bar():
            assert True

        @pytest.mark.isolated(group="mygroup")
        def test_baz():
            assert True
    """
    )

    # Complex expression: foo or bar (but not baz)
    result = pytester.runpytest("-v", "-k", "foo or bar")
    result.assert_outcomes(passed=2)


def test_k_filtering_with_isolated_flag(pytester: Pytester):
    """Test that -k option works when using --isolated flag."""
    pytester.makepyfile(
        """
        def test_match_this():
            assert True

        def test_skip_this():
            assert True
    """
    )

    # Use --isolated flag and -k filtering
    result = pytester.runpytest("-v", "--isolated", "-k", "match")
    result.assert_outcomes(passed=1)


def test_last_failed_option(pytester: Pytester):
    """Test that --lf (last-failed) option works with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_isolated_pass():
            assert True

        @pytest.mark.isolated
        def test_isolated_fail():
            assert False

        def test_normal_pass():
            assert True
    """
    )

    # First run - one test fails
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=1)

    # Second run with --lf - should only run the failed test
    result = pytester.runpytest("-v", "--lf")
    result.assert_outcomes(passed=0, failed=1)


def test_failed_first_option(pytester: Pytester):
    """Test that --ff (failed-first) option works with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_isolated_1():
            assert True

        @pytest.mark.isolated
        def test_isolated_2():
            assert False

        @pytest.mark.isolated
        def test_isolated_3():
            assert True
    """
    )

    # First run - one test fails
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2, failed=1)

    # Second run with --ff - all tests run but failed one first
    result = pytester.runpytest("-v", "--ff")
    result.assert_outcomes(passed=2, failed=1)

    # Verify order by checking the output
    # The failed test should appear first in output (at 33% not 66%)
    output = result.stdout.str()
    assert "test_isolated_2 FAILED" in output
    # Find position of each test in output
    pos_2 = output.find("test_isolated_2 FAILED")
    pos_1 = output.find("test_isolated_1 PASSED")
    pos_3 = output.find("test_isolated_3 PASSED")
    # test_isolated_2 should come before test_isolated_1 and test_isolated_3
    assert pos_2 < pos_1, "Failed test should run first"
    assert pos_2 < pos_3, "Failed test should run first"


def test_marker_filtering(pytester: Pytester):
    """Test that -m (marker) filtering works with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        @pytest.mark.slow
        def test_isolated_slow():
            assert True

        @pytest.mark.isolated
        @pytest.mark.fast
        def test_isolated_fast():
            assert True

        @pytest.mark.slow
        def test_normal_slow():
            assert True
    """
    )

    # Filter for only "fast" tests
    result = pytester.runpytest("-v", "-m", "fast")
    result.assert_outcomes(passed=1)

    # Filter for only "slow" tests
    result = pytester.runpytest("-v", "-m", "slow")
    result.assert_outcomes(passed=2)


def test_combined_k_and_m_filtering(pytester: Pytester):
    """Test that -k and -m options work together with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        @pytest.mark.unit
        def test_isolated_unit_match():
            assert True

        @pytest.mark.isolated
        @pytest.mark.integration
        def test_isolated_integration_match():
            assert True

        @pytest.mark.isolated
        @pytest.mark.unit
        def test_isolated_unit_skip():
            assert True
    """
    )

    # Combine -k and -m filters
    result = pytester.runpytest("-v", "-m", "unit", "-k", "match")
    result.assert_outcomes(passed=1)
