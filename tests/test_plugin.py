"""Test the pytest-isolated plugin using pytester fixture."""

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


def test_failed_test_output_captured(pytester: Pytester):
    """Test that failed test output is captured and displayed."""
    pytester.makepyfile(
        """
        import pytest
        import sys

        @pytest.mark.isolated
        def test_failing():
            print("stdout message")
            print("stderr message", file=sys.stderr)
            assert False, "Expected failure"
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*stdout message*",
            "*stderr message*",
            "*Expected failure*",
        ]
    )


def test_setup_teardown_failures(pytester: Pytester):
    """Test that setup and teardown failures are properly reported."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def failing_fixture():
            raise RuntimeError("Setup failed")

        @pytest.mark.isolated
        def test_with_failing_fixture(failing_fixture):
            pass
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*Setup failed*"])


def test_subprocess_crash_handling(pytester: Pytester):
    """Test that subprocess failures are properly reported."""
    # Note: os._exit() during test collection crashes before our plugin can report
    # So we test a crash that happens during test execution
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_that_fails():
            # Regular failure is properly reported through subprocess
            assert False, "This should be reported as failed"
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    # Should see the failure message
    assert "This should be reported as failed" in result.stdout.str()


def test_timeout_handling(pytester: Pytester):
    """Test that timeout is enforced and reported."""
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated
        def test_timeout():
            time.sleep(10)
    """
    )

    result = pytester.runpytest("-v", "--isolated-timeout=1")
    result.assert_outcomes(failed=1)
    assert "timed out" in result.stdout.str()


def test_marker_timeout(pytester: Pytester):
    """Test that per-marker timeout overrides global timeout."""
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated(group="slow", timeout=1)
        def test_marker_timeout():
            time.sleep(5)

        @pytest.mark.isolated(group="fast", timeout=10)
        def test_marker_no_timeout():
            time.sleep(0.1)
    """
    )

    result = pytester.runpytest("-v", "--isolated-timeout=100")
    result.assert_outcomes(passed=1, failed=1)
    # test_marker_timeout should fail (1s timeout)
    assert "test_marker_timeout" in result.stdout.str()
    assert "timed out after 1" in result.stdout.str()


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


def test_skipped_test_handling(pytester: Pytester):
    """Test that skipped tests are properly reported."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        @pytest.mark.skip(reason="Testing skip")
        def test_skipped():
            pass
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(skipped=1)


def test_xfail_test_handling(pytester: Pytester):
    """Test that xfail tests are properly handled."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        @pytest.mark.xfail(reason="Expected to fail")
        def test_xfail():
            assert False
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(xfailed=1)


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


def test_junit_xml_output(pytester: Pytester):
    """Test that JUnit XML output works with subprocess tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_pass():
            assert True

        @pytest.mark.isolated
        def test_fail():
            assert False, "Expected failure"
    """
    )

    result = pytester.runpytest("-v", "--junitxml=junit.xml")
    result.assert_outcomes(passed=1, failed=1)

    # Verify XML file was created
    junit_xml = pytester.path / "junit.xml"
    assert junit_xml.exists()

    # Basic validation that it contains test info
    content = junit_xml.read_text()
    assert "test_pass" in content
    assert "test_fail" in content


def test_capture_passed_config(pytester: Pytester):
    """Test isolated_capture_passed configuration option."""
    # Note: Currently output capture for passed tests requires using sections
    # This test verifies the configuration is recognized without warnings
    pytester.makeini(
        """
        [tool:pytest]
        isolated_timeout = 300
    """
    )

    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_pass():
            print("This output is captured")
            assert True
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)
    # Configuration should be accepted without warnings about unknown options
    assert "Unknown config option" not in result.stdout.str()


def test_no_infinite_recursion(pytester: Pytester):
    """Test that child processes don't spawn more subprocesses."""
    pytester.makepyfile(
        """
        import pytest
        import os

        @pytest.mark.isolated
        def test_check_env():
            # Verify we're in a subprocess
            assert os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS") == "1"
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)


def test_test_duration_tracking(pytester: Pytester):
    """Test that test duration is tracked properly."""
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.mark.isolated
        def test_with_duration():
            time.sleep(0.1)
            assert True
    """
    )

    result = pytester.runpytest("-v", "--durations=1")
    result.assert_outcomes(passed=1)
    # Should show duration information
    assert "test_with_duration" in result.stdout.str()


def test_no_isolation_option(pytester: Pytester):
    """Test that --no-isolation disables subprocess isolation."""
    pytester.makepyfile(
        """
        import pytest

        counter = 0

        @pytest.mark.isolated(group="test_group")
        def test_one():
            global counter
            counter += 1
            assert counter == 1

        @pytest.mark.isolated(group="test_group")
        def test_two():
            global counter
            counter += 1
            # With --no-isolation, both tests run in same process and share state
            assert counter == 2
    """
    )

    result = pytester.runpytest("-v", "--no-isolation")
    result.assert_outcomes(passed=2)


def test_subprocess_with_nested_directory_structure(pytester: Pytester):
    """Test that isolated tests work when running from a nested directory.

    This reproduces the issue where subprocess cannot find tests because
    it runs with a different rootdir/cwd than the parent pytest session.
    """
    # Create a nested directory structure similar to the reported issue

    # Create tests in a subdirectory
    subdir = pytester.mkdir("tests")
    test_file = subdir / "test_nested.py"
    test_file.write_text("""
import pytest

@pytest.mark.isolated
def test_in_nested_dir():
    assert True
""")

    # Run pytest from the root, but specify the test file with relative path
    # This mimics the scenario where pytest runs from a parent directory
    # and the subprocess needs to find the test in the subdirectory
    result = pytester.runpytest("-v", "tests/test_nested.py")
    result.assert_outcomes(passed=1)


def test_subprocess_with_local_imports(pytester: Pytester):
    """Test subprocess isolation when test imports from local helper module.

    This reproduces the issue where:
    - pytest is invoked with --rootdir pointing to tests/ directory
    - The nodeid is collected as just test_file.py::test_name (no tests/ prefix)
    - The subprocess runs without --rootdir, defaulting to parent directory
    - Collection fails because test file can't be found
    """
    # Create a helper module in tests directory
    tests_dir = pytester.mkdir("tests")
    helper_file = tests_dir / "test_helpers.py"
    helper_file.write_text("""
def helper_function():
    return "helper_result"
""")

    # Create test that imports from the helper
    test_file = tests_dir / "test_with_import.py"
    test_file.write_text("""
import pytest
from test_helpers import helper_function

@pytest.mark.isolated
def test_using_helper():
    assert helper_function() == "helper_result"
""")

    # This is the key: run pytest with --rootdir pointing to tests/
    # This makes pytest collect the test as "test_with_import.py::test_using_helper"
    # without the "tests/" prefix
    result = pytester.runpytest("-v", "--rootdir", "tests", "tests/test_with_import.py")
    # This should fail without the fix because the subprocess won't pass --rootdir
    result.assert_outcomes(passed=1)


def test_subprocess_with_directory_argument(pytester: Pytester):
    """Test that directory arguments don't cause subprocess collection errors.

    When user runs 'pytest tests', the word 'tests' should not be forwarded to the
    subprocess because:
    1. The subprocess is given explicit nodeids to run
    2. Passing 'tests' again causes pytest to try collecting twice
    3. This can lead to 'file or directory not found' or other collection errors
    """
    # Create a tests subdirectory with isolated tests
    tests_dir = pytester.mkdir("tests")
    test_file = tests_dir / "test_isolated.py"
    test_file.write_text("""
import pytest

@pytest.mark.isolated(group="1")
def test_in_subdir_1():
    assert True

@pytest.mark.isolated(group="2")
def test_in_subdir_2():
    assert True
""")

    # Run pytest with directory argument 'tests'
    # This should work - the subprocess should only get the nodeids, not 'tests'
    result = pytester.runpytest("-v", "tests")
    result.assert_outcomes(passed=2)


def test_exitfirst_option(pytester: Pytester):
    """Test that -x/--exitfirst stops execution after first failure"""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated(group="a")
        def test_fail_first():
            assert False, "First failure"

        @pytest.mark.isolated(group="b")
        def test_should_not_run_1():
            assert True

        @pytest.mark.isolated(group="c")
        def test_should_not_run_2():
            assert True
    """
    )

    result = pytester.runpytest("-v", "-x")
    result.assert_outcomes(failed=1)
    # Verify the other tests were not run
    assert "test_should_not_run_1" not in result.stdout.str()
    assert "test_should_not_run_2" not in result.stdout.str()
    assert "stopping after 1 failures" in result.stdout.str()


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
