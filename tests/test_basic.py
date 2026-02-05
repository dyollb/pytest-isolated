"""Basic isolation and grouping tests.

Tests core functionality of running tests in isolated subprocesses and basic grouping.
"""

from pytest import Pytester


def test_basic_subprocess_isolation(pytester: Pytester):
    """Test that subprocess marker runs tests in isolation when in different groups."""
    pytester.makepyfile(
        """
        import pytest

        counter = 0

        @pytest.mark.isolated(group="group1")
        def test_isolated_1():
            global counter
            counter += 1
            assert counter == 1  # Would fail if sharing state

        @pytest.mark.isolated(group="group2")
        def test_isolated_2():
            global counter
            counter += 1
            assert counter == 1  # Fresh state in different group
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)


def test_grouped_subprocess(pytester: Pytester):
    """Test that tests with same group run in one subprocess and share state."""
    pytester.makepyfile(
        """
        import pytest

        shared_state = []

        @pytest.mark.isolated(group="mygroup")
        def test_group_1():
            shared_state.append(1)
            assert len(shared_state) == 1

        @pytest.mark.isolated(group="mygroup")
        def test_group_2():
            shared_state.append(2)
            assert len(shared_state) == 2  # Should see previous state
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)


def test_different_groups_isolated(pytester: Pytester):
    """Test that different groups run in separate subprocesses."""
    pytester.makepyfile(
        """
        import pytest

        counter = 0

        @pytest.mark.isolated(group="group1")
        def test_group1_a():
            global counter
            counter += 1
            assert counter == 1

        @pytest.mark.isolated(group="group1")
        def test_group1_b():
            global counter
            counter += 1
            assert counter == 2  # Sees previous state in same group

        @pytest.mark.isolated(group="group2")
        def test_group2_a():
            global counter
            counter += 1
            assert counter == 1  # Fresh state in different group
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)


def test_mixed_subprocess_and_normal(pytester: Pytester):
    """Test that subprocess and normal tests can coexist."""
    pytester.makepyfile(
        """
        import pytest

        # Note: When testing with pytester, this file runs in its own process
        # So we just verify that mixing subprocess and normal tests works

        @pytest.mark.isolated
        def test_isolated():
            assert True

        def test_normal_1():
            assert True

        def test_normal_2():
            assert True
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)
