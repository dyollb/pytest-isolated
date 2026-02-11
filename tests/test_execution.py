"""Subprocess execution tests.

Tests subprocess management, crash detection, timeout handling, and execution flow.
"""

import textwrap

from pytest import Pytester


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


def test_subprocess_crash_during_test_execution(pytester: Pytester):
    """Test that subprocess crash during test execution is reported as failure.

    When a test causes a process crash (via os.abort()), the subprocess
    dies mid-execution. The plugin should detect this and report the test as
    failed with an informative error message.

    os.abort() works cross-platform: on Unix it sends SIGABRT (signal 6),
    on Windows it terminates the process abnormally with exit code 3.
    """
    pytester.makepyfile(
        """
        import os
        import pytest

        @pytest.mark.isolated
        def test_crash():
            # Trigger an abnormal process termination
            os.abort()
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    # Should see crash information
    stdout = result.stdout.str()
    # On Unix: "crashed with signal", on Windows: "crashed with exit code"
    assert "crashed with signal" in stdout or "crashed with exit code" in stdout


def test_subprocess_crash_with_multiple_tests_in_group(pytester: Pytester):
    """Test that when one test crashes, remaining tests in group are reported.

    If a group contains multiple tests and one crashes, the remaining tests
    should be marked as 'not run' rather than silently disappearing.
    """
    pytester.makepyfile(
        """
        import os
        import pytest

        @pytest.mark.isolated(group="crashgroup")
        def test_before_crash():
            assert True

        @pytest.mark.isolated(group="crashgroup")
        def test_crash():
            os.abort()

        @pytest.mark.isolated(group="crashgroup")
        def test_after_crash():
            # This test should be reported as not run
            assert True
    """
    )

    result = pytester.runpytest("-v")
    # test_before_crash passes, test_crash fails, test_after_crash fails (not run)
    result.assert_outcomes(passed=1, failed=2)
    stdout = result.stdout.str()
    assert "test_crash" in stdout
    assert "crashed with signal" in stdout or "crashed with exit code" in stdout


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


def test_fixture_teardown_called_on_timeout(pytester: Pytester):
    """Test that fixture setup and test start are captured even when test times out.

    Note: When a test times out, the subprocess is killed and fixture teardown
    does not run. This test verifies that output before the timeout is captured.
    """
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.fixture
        def my_fixture():
            print("FIXTURE_SETUP_COMPLETE")
            yield "resource"
            print("FIXTURE_TEARDOWN_CALLED")

        @pytest.mark.isolated
        def test_timeout_with_fixture(my_fixture):
            print("TEST_STARTED")
            time.sleep(10)
    """
    )

    result = pytester.runpytest("-v", "-s", "--isolated-timeout=1")
    result.assert_outcomes(failed=1)
    stdout = result.stdout.str()
    assert "timed out" in stdout
    assert "FIXTURE_SETUP_COMPLETE" in stdout
    assert "TEST_STARTED" in stdout
    # Teardown won't run because the process is killed on timeout


def test_fixture_itself_times_out(pytester: Pytester):
    """Test that fixture setup timeout is properly reported."""
    pytester.makepyfile(
        """
        import pytest
        import time

        @pytest.fixture
        def slow_fixture():
            print("FIXTURE_SETUP_STARTING")
            time.sleep(10)
            print("FIXTURE_SETUP_COMPLETE")
            yield "resource"
            print("FIXTURE_TEARDOWN_CALLED")

        @pytest.mark.isolated
        def test_with_slow_fixture(slow_fixture):
            print("TEST_BODY_REACHED")
            assert True
    """
    )

    result = pytester.runpytest("-v", "-s", "--isolated-timeout=1")
    result.assert_outcomes(failed=1)
    stdout = result.stdout.str()
    assert "timed out" in stdout
    assert "FIXTURE_SETUP_STARTING" in stdout
    # These should NOT be in the output since the fixture times out during setup
    assert "FIXTURE_SETUP_COMPLETE" not in stdout
    assert "TEST_BODY_REACHED" not in stdout


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
    test_file.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.isolated(group="1")
            def test_in_subdir_1():
                assert True

            @pytest.mark.isolated(group="2")
            def test_in_subdir_2():
                assert True
            """
        )
    )

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
