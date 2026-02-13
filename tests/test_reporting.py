"""Test result reporting tests.

Tests output capture, JUnit XML reporting, and test result handling.
"""

from pytest import Pytester


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


def test_capture_no_option_hides_passed_output(pytester: Pytester):
    """Test that passed test output is hidden by default (pytest standard behavior)."""
    pytester.makepyfile(
        """
        import pytest
        import sys

        @pytest.mark.isolated
        def test_pass():
            print("stdout from passing test")
            print("stderr from passing test", file=sys.stderr)
            assert True
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)
    # Output from passing tests should NOT be shown by default
    assert "stdout from passing test" not in result.stdout.str()
    assert "stderr from passing test" not in result.stdout.str()


def test_capture_flag_s_disables_capture(pytester: Pytester):
    """Test that -s flag is forwarded to subprocess and disables capture."""
    pytester.makepyfile(
        """
        import pytest
        import sys
        from pathlib import Path

        @pytest.mark.isolated
        def test_check_s_flag():
            # Write sys.argv to a file so we can verify -s was forwarded
            Path("subprocess_args.txt").write_text(str(sys.argv))
            print("output with -s flag")
            assert True
    """
    )

    result = pytester.runpytest("-v", "-s")
    result.assert_outcomes(passed=1)

    # Verify -s flag was forwarded to subprocess
    args_file = pytester.path / "subprocess_args.txt"
    assert args_file.exists()
    args_content = args_file.read_text()
    assert "-s" in args_content


def test_capture_flag_forwarded_to_subprocess(pytester: Pytester):
    """Test that capture works in subprocess even when parent uses --capture.

    Note: --capture is NOT forwarded to child (child always uses tee-sys for
    timeout handling), but the parent's --capture setting still controls what
    the user sees in the final output.
    """
    pytester.makepyfile(
        """
        import pytest
        import sys
        from pathlib import Path

        @pytest.mark.isolated
        def test_check_capture_behavior():
            # Write sys.argv to verify child uses tee-sys (not parent's capture mode)
            Path("subprocess_args.txt").write_text(str(sys.argv))
            # Print output that should be captured
            print("captured output")
            assert True
    """
    )

    result = pytester.runpytest("-v", "--capture=sys")
    result.assert_outcomes(passed=1)

    # Verify child used tee-sys, not the parent's --capture=sys
    args_file = pytester.path / "subprocess_args.txt"
    assert args_file.exists()
    args_content = args_file.read_text()
    assert "--capture=tee-sys" in args_content
    assert "--capture=sys" not in args_content  # Parent's flag NOT forwarded

    # Verify capture still works - output should NOT appear for passed test
    # (parent's --capture=sys controls final output visibility)
    assert "captured output" not in result.stdout.str()


def test_capture_output_behavior_failed_test(pytester: Pytester):
    """Test that failed test output is shown regardless of capture mode."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_fail():
            print("output from failed test")
            assert False, "intentional failure"
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    # Failed test output should always be shown
    assert "output from failed test" in result.stdout.str()


def test_capture_output_behavior_passed_test_default(pytester: Pytester):
    """Test that passed test output is hidden by default."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_pass():
            print("output from passed test")
            assert True
    """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)
    # Passed test output should NOT be shown with default capture
    output = result.stdout.str()
    assert "output from passed test" not in output


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
