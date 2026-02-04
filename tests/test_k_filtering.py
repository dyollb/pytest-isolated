"""Test that -k filtering works with pytest-isolated plugin."""

from pytest import Pytester


def test_k_filtering_with_isolated_tests(pytester: Pytester):
    """Test that -k option filters isolated tests correctly."""
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

    # Verify only foo tests ran
    stdout = result.stdout.str()
    assert "test_foo_one" in stdout
    assert "test_foo_two" in stdout
    assert "test_bar_one" not in stdout
    assert "test_bar_two" not in stdout


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
    # Note: deselected count may not be reported when using custom test loops
    result.assert_outcomes(passed=2)

    # Verify only match tests ran
    stdout = result.stdout.str()
    assert "test_isolated_match" in stdout
    assert "test_normal_match" in stdout
    assert "test_isolated_nomatch" not in stdout
    assert "test_normal_nomatch" not in stdout


def test_k_filtering_complex_expression(pytester: Pytester):
    """Test that -k option with complex expressions works correctly."""
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

    # Filter for "foo or bar" (not baz)
    result = pytester.runpytest("-v", "-k", "foo or bar")
    result.assert_outcomes(passed=2)

    # Verify only foo and bar ran
    stdout = result.stdout.str()
    assert "test_foo" in stdout
    assert "test_bar" in stdout
    assert "test_baz" not in stdout


def test_k_filtering_with_isolated_flag(pytester: Pytester):
    """Test that -k option works with --isolated flag."""
    pytester.makepyfile(
        """
        def test_alpha():
            assert True

        def test_beta():
            assert True

        def test_gamma():
            assert True
    """
    )

    # Run with --isolated and -k filter
    result = pytester.runpytest("-v", "--isolated", "-k", "alpha or beta")
    result.assert_outcomes(passed=2)

    # Verify only alpha and beta ran
    stdout = result.stdout.str()
    assert "test_alpha" in stdout
    assert "test_beta" in stdout
    assert "test_gamma" not in stdout
