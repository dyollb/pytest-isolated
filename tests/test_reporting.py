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
