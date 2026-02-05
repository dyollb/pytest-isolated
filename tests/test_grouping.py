"""Test grouping logic.

Tests how pytest-isolated groups tests based on markers, classes, and modules.
"""

from pytest import Pytester


def test_default_grouping_by_module(pytester: Pytester):
    """Test that tests without explicit group each run in their own subprocess."""
    pytester.makepyfile(
        test_mod1="""
        import pytest

        state = []

        @pytest.mark.isolated
        def test_a():
            state.append(1)
            assert len(state) == 1

        @pytest.mark.isolated
        def test_b():
            state.append(2)
            assert len(state) == 1  # Different subprocess, fresh state
    """,
        test_mod2="""
        import pytest

        state = []

        @pytest.mark.isolated
        def test_c():
            state.append(1)
            assert len(state) == 1  # Different subprocess, fresh state
    """,
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)


def test_class_marker_grouping(pytester: Pytester):
    """Test that class-level @pytest.mark.isolated groups all methods together."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        class TestDB:
            shared = []

            def test_a(self):
                self.shared.append("a")
                assert len(self.shared) == 1

            def test_b(self):
                self.shared.append("b")
                assert len(self.shared) == 2  # Same subprocess, shared state

            def test_c(self):
                self.shared.append("c")
                assert len(self.shared) == 3  # Same subprocess, shared state
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)


def test_parametrized_tests(pytester: Pytester):
    """Test that parametrized tests work in subprocess."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated(group="params")
        @pytest.mark.parametrize("value", [1, 2, 3])
        def test_param(value):
            assert value in [1, 2, 3]
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)


def test_positional_group_argument(pytester: Pytester):
    """Test that @pytest.mark.isolated("groupname") positional syntax works."""
    pytester.makepyfile(
        """
        import pytest

        shared = []

        @pytest.mark.isolated("shared_group")
        def test_first():
            shared.append(1)
            assert len(shared) == 1

        @pytest.mark.isolated("shared_group")
        def test_second():
            shared.append(2)
            assert len(shared) == 2  # Same group, shared state
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)


def test_isolated_flag_runs_all_tests(pytester: Pytester):
    """Test that --isolated flag runs all tests in isolation."""
    pytester.makepyfile(
        """
        counter = 0

        def test_normal_1():
            global counter
            counter += 1
            assert counter == 1  # Would fail if sharing state

        def test_normal_2():
            global counter
            counter += 1
            assert counter == 1  # Should have fresh state with --isolated
    """
    )

    result = pytester.runpytest("-v", "--isolated")
    result.assert_outcomes(passed=2)


def test_module_marker_groups_all_functions(pytester: Pytester):
    """Test that pytestmark at module level groups all functions together."""
    pytester.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.isolated

        shared = []

        def test_first():
            shared.append(1)
            assert len(shared) == 1

        def test_second():
            shared.append(2)
            assert len(shared) == 2  # Same module, shared subprocess

        def test_third():
            shared.append(3)
            assert len(shared) == 3  # Same module, shared subprocess
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=3)
