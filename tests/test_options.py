"""CLI options and configuration tests.

Tests command-line options and plugin configuration.
"""

import textwrap

from pytest import Pytester


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
    test_file.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.isolated
            def test_in_nested_dir():
                assert True
            """
        )
    )

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
    helper_file.write_text(
        textwrap.dedent(
            """
            def helper_function():
                return "helper_result"
            """
        )
    )

    # Create test that imports from the helper
    test_file = tests_dir / "test_with_import.py"
    test_file.write_text(
        textwrap.dedent(
            """
            import pytest
            from test_helpers import helper_function

            @pytest.mark.isolated
            def test_using_helper():
                assert helper_function() == "helper_result"
            """
        )
    )

    # This is the key: run pytest with --rootdir pointing to tests/
    # This makes pytest collect the test as "test_with_import.py::test_using_helper"
    # without the "tests/" prefix
    result = pytester.runpytest("-v", "--rootdir", "tests", "tests/test_with_import.py")
    # This should fail without the fix because the subprocess won't pass --rootdir
    result.assert_outcomes(passed=1)
