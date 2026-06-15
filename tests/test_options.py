"""CLI options and configuration tests.

Tests command-line options and plugin configuration.
"""

import contextlib
import os
import sys
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


def test_pdb_with_isolated_tests(pytester: Pytester):
    """Test that --pdb with isolated tests exits with a UsageError (issue #34).

    When --pdb is used with isolated tests, pytest should refuse to run
    because pdb cannot work in subprocesses. The error message should
    suggest using --no-isolation --pdb explicitly.
    """
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_good1():
            assert True

        @pytest.mark.isolated
        def test_good2():
            assert True
    """
    )

    # Run with --pdb flag - should exit with an error
    result = pytester.runpytest("-v", "--pdb")

    # pytest should have exited with an error code
    assert result.ret != 0

    # Check that the error message mentions --no-isolation
    full_output = result.stdout.str() + result.stderr.str()
    assert "--no-isolation" in full_output, (
        f"Expected error to mention --no-isolation flag:\n{full_output}"
    )
    assert "--pdb" in full_output, f"Expected error to mention --pdb:\n{full_output}"


def test_no_isolation_with_pdb_no_error(pytester: Pytester):
    """Test that --no-isolation --pdb together works without error.

    When users explicitly use --no-isolation with --pdb, there should be
    no error since they've opted out of isolation.
    """
    pytester.makepyfile(
        """
        import pytest

        counter = 0

        @pytest.mark.isolated
        def test_good1():
            global counter
            counter += 1
            assert counter == 1

        @pytest.mark.isolated
        def test_good2():
            global counter
            counter += 1
            assert counter == 2
    """
    )

    # Run with both --no-isolation and --pdb - should work fine
    result = pytester.runpytest("-v", "--no-isolation", "--pdb")

    # Both tests should pass (sharing state, no isolation)
    result.assert_outcomes(passed=2)


def test_failed_first_with_isolated_tests(pytester: Pytester):
    """Test that --ff (failed first) reorders isolated tests (issue #34).

    The --ff flag should run previously failed tests first, even for isolated tests.
    """
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_good1():
            assert True

        @pytest.mark.isolated
        def test_fail():
            assert False, "This test fails"

        @pytest.mark.isolated
        def test_good2():
            assert True
    """
    )

    # First run: let the test fail and record it in cache
    result1 = pytester.runpytest("-v")
    result1.assert_outcomes(passed=2, failed=1)

    # Second run with --ff: the failed test should run first
    result2 = pytester.runpytest("-v", "--ff")
    result2.assert_outcomes(passed=2, failed=1)

    # Verify that test_fail was run first in the second run
    output = result2.stdout.str()

    # Check for the "run-last-failure" message
    assert "run-last-failure" in output, (
        "Expected --ff to trigger run-last-failure mode"
    )

    # Verify test_fail ran first by checking output order
    assert "test_fail FAILED" in output
    # test_fail should appear before test_good1 in output since it ran first
    assert output.index("test_fail FAILED") < output.index("test_good1 PASSED"), (
        "test_fail should run before test_good1 with --ff"
    )


# Phase 1.5: Tests for PYTEST_ADDOPTS support and incompatible options


def test_custom_option_forwarded_to_subprocess(pytester: Pytester):
    """Test that custom pytest plugin options are forwarded to subprocess (issue #48).

    This tests the new blacklist-based forwarding approach: custom options should
    be forwarded by default unless explicitly blacklisted.
    """
    # Add a custom option via conftest
    conftest_code = textwrap.dedent(
        """
        import pytest

        def pytest_addoption(parser):
            parser.addoption(
                "--eval-solution-path",
                action="store",
                default=None,
                help="Path to evaluation solution",
            )

        @pytest.fixture
        def eval_path(request):
            return request.config.getoption("--eval-solution-path")
        """
    )
    pytester.makeconftest(conftest_code)

    pytester.makepyfile(
        """
        import pytest

        def test_non_isolated_with_option(eval_path):
            assert eval_path == "/path/to/solution", (
                f"Expected /path/to/solution, got {eval_path}"
            )

        @pytest.mark.isolated
        def test_isolated_with_option(eval_path):
            assert eval_path == "/path/to/solution", (
                f"Expected /path/to/solution, got {eval_path}"
            )
        """
    )

    # Run with the custom option
    result = pytester.runpytest("-v", "--eval-solution-path=/path/to/solution")
    result.assert_outcomes(passed=2)


def test_pytest_addopts_env_var_forwarded(pytester: Pytester):
    """Test that PYTEST_ADDOPTS environment variable is inherited in subprocess.

    PYTEST_ADDOPTS is a standard pytest feature for passing default options.
    The subprocess should inherit it from the parent's environment.
    """
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest

            def pytest_addoption(parser):
                parser.addoption(
                    "--custom-flag",
                    action="store_true",
                    default=False,
                )

            @pytest.fixture
            def custom_enabled(request):
                return request.config.getoption("--custom-flag")
            """
        )
    )

    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_with_env_option(custom_enabled):
            assert custom_enabled is True
        """
    )

    # Use monkeypatch to set PYTEST_ADDOPTS in the environment
    original_addopts = os.environ.get("PYTEST_ADDOPTS")
    try:
        os.environ["PYTEST_ADDOPTS"] = "--custom-flag"
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=1)
    finally:
        if original_addopts is None:
            os.environ.pop("PYTEST_ADDOPTS", None)
        else:
            os.environ["PYTEST_ADDOPTS"] = original_addopts


def test_incompatible_option_collect_only_error(pytester: Pytester):
    """Test that --collect-only is supported with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_example():
            assert True
        """
    )

    # Run with --collect-only - should succeed (parent-handled, no forwarding)
    result = pytester.runpytest("-v", "--collect-only")
    assert result.ret == 0
    output = result.stdout.str()
    assert "collected" in output


def test_incompatible_option_lf_error(pytester: Pytester):
    """Test that --lf (last-failed) is supported with isolated tests."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_example():
            assert True
        """
    )

    # Run with --lf - should succeed (handled by parent process)
    result = pytester.runpytest("-v", "--lf")
    assert result.ret == 0


def test_incompatible_option_with_no_isolation_allowed(pytester: Pytester):
    """Test that incompatible options are allowed when --no-isolation is used.

    When users opt out of isolation, they should be able to use any option,
    including those that are incompatible with isolation.
    """
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.isolated
        def test_example():
            assert True
        """
    )

    # Run with --no-isolation --pdb - should NOT error
    # (--pdb is normally incompatible, but allowed with --no-isolation)
    result = pytester.runpytest("-v", "--no-isolation", "--pdb")
    # With --no-isolation, we skip tests that require interaction
    # So exit code 0 is expected (no tests actually run with pdb without input)
    assert result.ret == 0


def test_setup_show_with_isolated_tests(pytester: Pytester):
    """Test that --setup-show displays fixture setup for isolated tests (#47)."""
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def some_fixture():
            return "value"

        @pytest.mark.isolated
        def test_isolated_with_fixture(some_fixture):
            assert some_fixture == "value"
        """
    )

    result = pytester.runpytest("-v", "--setup-show")
    result.assert_outcomes(passed=1)
    output = result.stdout.str()
    assert "SETUP" in output
    assert "some_fixture" in output


def test_p_option_plugin_loaded_in_isolated_subprocess(pytester: Pytester):
    """Test that -p loaded plugins are available in isolated subprocesses."""

    pytester.path.joinpath("myplugin.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.fixture
            def plugin_fixture():
                return "from-plugin"
            """
        )
    )
    pytester.makepyfile(
        test_sample="""
        import pytest

        @pytest.mark.isolated
        def test_uses_plugin_fixture(plugin_fixture):
            assert plugin_fixture == "from-plugin"
        """,
    )

    original_pythonpath = os.environ.get("PYTHONPATH")
    inserted_path = str(pytester.path)
    os.environ["PYTHONPATH"] = (
        inserted_path
        if not original_pythonpath
        else f"{inserted_path}{os.pathsep}{original_pythonpath}"
    )
    sys.path.insert(0, inserted_path)
    try:
        result = pytester.runpytest("-v", "-p", "myplugin")
        result.assert_outcomes(passed=1)
    finally:
        if original_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = original_pythonpath
        with contextlib.suppress(ValueError):
            sys.path.remove(inserted_path)


def test_continue_on_collection_errors_with_isolated_tests(pytester: Pytester):
    """Test that parent collection can continue and still run isolated tests."""
    pytester.makepyfile(
        test_broken="""
        raise RuntimeError("collection boom")
        """,
        test_isolated_ok="""
        import pytest

        @pytest.mark.isolated
        def test_ok():
            assert True
        """,
    )

    result = pytester.runpytest("-v", "--continue-on-collection-errors")
    output = result.stdout.str() + result.stderr.str()
    assert "test_ok PASSED" in output
    assert "collection boom" in output


def test_import_mode_forwarded_to_isolated_subprocess(pytester: Pytester):
    """Test that --import-mode is forwarded to isolated subprocesses."""
    pkg = pytester.mkdir("pkg")
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text(
        textwrap.dedent(
            """
            def helper_value():
                return "ok"
            """
        )
    )
    (pkg / "test_import_mode.py").write_text(
        textwrap.dedent(
            """
            import pytest
            from .helpers import helper_value

            @pytest.mark.isolated
            def test_uses_relative_import():
                assert helper_value() == "ok"
            """
        )
    )

    result = pytester.runpytest(
        "-v", "--import-mode=importlib", "pkg/test_import_mode.py"
    )
    result.assert_outcomes(passed=1)
