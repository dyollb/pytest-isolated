"""Test grouping logic.

Tests how pytest-isolated groups tests based on markers, classes, and modules.
"""

import xml.etree.ElementTree as ET

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


def test_module_marker_failing_test_log_in_correct_node(pytester: Pytester):
    """Test that failing test with module marker attaches log to correct test node.

    Issue #33: Module-level marker should not break how/where the log is shown.
    The failure output should be associated with the specific test that failed.
    """
    pytester.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.isolated

        def test_pass():
            print("output from passing test")
            assert True

        def test_fail():
            print("output from failing test")
            assert False, "Expected failure"

        def test_pass_again():
            print("output from another passing test")
            assert True
    """
    )

    result = pytester.runpytest("-v", "--junitxml=junit.xml")
    result.assert_outcomes(passed=2, failed=1)

    # Parse the JUnit XML
    junit_xml = pytester.path / "junit.xml"
    assert junit_xml.exists()
    tree = ET.parse(junit_xml)
    root = tree.getroot()

    # Find all testcase elements
    testcases = root.findall(".//testcase")
    assert len(testcases) == 3, f"Expected 3 test cases, found {len(testcases)}"

    # Build a mapping of test name to testcase element
    tests_by_name = {tc.attrib["name"]: tc for tc in testcases}

    # Verify all three tests are present
    assert "test_pass" in tests_by_name
    assert "test_fail" in tests_by_name
    assert "test_pass_again" in tests_by_name

    # Check test_pass: should have no failure/error children
    test_pass = tests_by_name["test_pass"]
    assert test_pass.find("failure") is None, "test_pass should not have a failure"
    assert test_pass.find("error") is None, "test_pass should not have an error"

    # Check test_fail: should have a failure child with the correct message
    test_fail = tests_by_name["test_fail"]
    failure = test_fail.find("failure")
    assert failure is not None, "test_fail should have a failure element"
    # Check both .text and attrib["message"] — pytest puts the short message
    # in the attribute and the full traceback in the element text, but which
    # one carries the assertion message can vary by junit_family / version.
    failure_content = (failure.text or "") + failure.attrib.get("message", "")
    assert "Expected failure" in failure_content, (
        f"Failure should contain 'Expected failure', got: {failure_content}"
    )
    # Verify stdout is captured
    system_out = test_fail.find("system-out")
    if system_out is not None and system_out.text:
        assert "output from failing test" in system_out.text

    # Check test_pass_again: should have no failure/error children
    test_pass_again = tests_by_name["test_pass_again"]
    assert test_pass_again.find("failure") is None, (
        "test_pass_again should not have a failure"
    )
    assert test_pass_again.find("error") is None, (
        "test_pass_again should not have an error"
    )

    # Verify console output shows failure for the correct test
    output = result.stdout.str()
    assert "test_fail" in output
    assert "output from failing test" in output
    assert "Expected failure" in output


def test_overlapping_module_and_function_markers(pytester: Pytester):
    """Function marker under module pytestmark breaks out of module group.

    Precedence rule 2: closest marker wins — a function-level
    ``@pytest.mark.isolated`` gets its own subprocess even inside
    a module that already carries ``pytestmark``.
    """
    pytester.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.isolated

        shared = []

        def test_module_marker_only():
            shared.append("a")
            assert True

        @pytest.mark.isolated
        def test_both_markers():
            # Function marker wins over module scope.
            # Runs in its own subprocess (no shared state).
            assert len(shared) == 0

        @pytest.mark.isolated(group="custom")
        def test_both_markers_with_group():
            # Explicit group on function marker wins over module scope.
            # Runs in its own subprocess.
            assert len(shared) == 0
    """
    )

    result = pytester.runpytest("-v")

    # Tests should pass (each runs only once)
    result.assert_outcomes(passed=3)

    # Check that each test appears only once in output
    output = result.stdout.str()
    assert output.count("test_module_marker_only PASSED") == 1
    assert output.count("test_both_markers PASSED") == 1
    assert output.count("test_both_markers_with_group PASSED") == 1


# ---------------------------------------------------------------------------
# Marker precedence tests
# ---------------------------------------------------------------------------


class TestMarkerPrecedenceClassScope:
    """Precedence rule: function marker wins over class marker."""

    def test_class_plus_bare_function_marker(self, pytester: Pytester):
        """Function @isolated under @isolated class breaks out."""
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.isolated
            class TestGroup:
                shared = []

                def test_class_only(self):
                    self.shared.append("a")
                    assert len(self.shared) == 1

                @pytest.mark.isolated
                def test_with_own_marker(self):
                    # Function marker wins; gets own subprocess.
                    self.shared.append("b")
                    assert len(self.shared) == 1
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_class_plus_function_marker_with_timeout(self, pytester: Pytester):
        """@isolated(timeout=…) on a method breaks out of class grouping."""
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.isolated
            class TestGroup:
                shared = []

                def test_fast(self):
                    self.shared.append("fast")
                    assert len(self.shared) == 1

                @pytest.mark.isolated(timeout=60)
                def test_slow(self):
                    # Function marker wins; gets own subprocess.
                    self.shared.append("slow")
                    assert len(self.shared) == 1
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_class_plus_function_marker_with_explicit_group(self, pytester: Pytester):
        """@isolated(group=…) on a method overrides class grouping."""
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.isolated
            class TestGroup:
                shared = []

                def test_stays_in_class(self):
                    self.shared.append("a")
                    assert len(self.shared) == 1

                @pytest.mark.isolated(group="solo")
                def test_explicit_group(self):
                    # Explicit group breaks out of class subprocess.
                    self.shared.append("b")
                    assert len(self.shared) == 1  # own subprocess
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)


class TestMarkerPrecedenceModuleScope:
    """Precedence rule: function marker wins over module marker."""

    def test_module_plus_bare_function_marker(self, pytester: Pytester):
        """Function @isolated under module pytestmark breaks out."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            shared = []

            def test_module_only():
                shared.append("a")
                assert len(shared) == 1

            @pytest.mark.isolated
            def test_with_own_marker():
                # Function marker wins; gets own subprocess.
                assert len(shared) == 0
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_module_plus_function_marker_with_timeout(self, pytester: Pytester):
        """@isolated(timeout=…) on a function breaks out of module grouping."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            shared = []

            def test_normal():
                shared.append("a")
                assert len(shared) == 1

            @pytest.mark.isolated(timeout=60)
            def test_with_timeout():
                # Function marker wins; gets own subprocess.
                assert len(shared) == 0
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_module_plus_function_marker_with_explicit_group(self, pytester: Pytester):
        """@isolated(group=…) on a function overrides module grouping."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            shared = []

            def test_stays_in_module():
                shared.append("a")
                assert len(shared) == 1

            @pytest.mark.isolated(group="breakout")
            def test_with_explicit_group():
                # Explicit group wins; gets own subprocess.
                shared.append("b")
                assert len(shared) == 1
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)


class TestMarkerPrecedenceMultiScope:
    """Precedence when module, class, and function markers all overlap."""

    def test_module_plus_class_groups_by_class(self, pytester: Pytester):
        """Class @isolated inside module pytestmark groups by class."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            module_shared = []

            def test_module_function():
                module_shared.append("mod")
                assert len(module_shared) == 1

            @pytest.mark.isolated
            class TestInner:
                class_shared = []

                def test_a(self):
                    self.class_shared.append("a")
                    assert len(self.class_shared) == 1

                def test_b(self):
                    self.class_shared.append("b")
                    assert len(self.class_shared) == 2  # same class group
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=3)

    def test_module_class_and_function_bare_marker(self, pytester: Pytester):
        """All three scopes: function marker wins, breaks out of class."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            @pytest.mark.isolated
            class TestTriple:
                shared = []

                def test_class_only(self):
                    self.shared.append("a")
                    assert len(self.shared) == 1

                @pytest.mark.isolated
                def test_all_three(self):
                    # Function marker wins; gets own subprocess.
                    self.shared.append("b")
                    assert len(self.shared) == 1
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_module_class_and_function_with_explicit_group(self, pytester: Pytester):
        """Explicit group on function wins over both class and module."""
        pytester.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.isolated

            @pytest.mark.isolated
            class TestTriple:
                shared = []

                def test_class_only(self):
                    self.shared.append("a")
                    assert len(self.shared) == 1

                @pytest.mark.isolated(group="breakout")
                def test_explicit_group(self):
                    # Explicit group wins; own subprocess.
                    self.shared.append("b")
                    assert len(self.shared) == 1
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)


class TestMarkerPrecedenceStandalone:
    """Standalone function markers (no parent isolation scope)."""

    def test_function_marker_only_gets_own_subprocess(self, pytester: Pytester):
        """@isolated on a function without class/module scope -> own group."""
        pytester.makepyfile(
            """
            import pytest

            shared = []

            @pytest.mark.isolated
            def test_a():
                shared.append("a")
                assert len(shared) == 1

            @pytest.mark.isolated
            def test_b():
                shared.append("b")
                assert len(shared) == 1  # own subprocess
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)

    def test_function_marker_with_timeout_gets_own_subprocess(self, pytester: Pytester):
        """@isolated(timeout=…) without parent scope -> own group."""
        pytester.makepyfile(
            """
            import pytest

            shared = []

            @pytest.mark.isolated(timeout=60)
            def test_a():
                shared.append("a")
                assert len(shared) == 1

            @pytest.mark.isolated
            def test_b():
                shared.append("b")
                assert len(shared) == 1  # different subprocess
        """
        )
        result = pytester.runpytest("-v")
        result.assert_outcomes(passed=2)
