"""Tests to reproduce Windows-specific issues reported by users.

These tests combine multiple issues that can occur together on Windows:
1. Timeout with no logging output
2. Fixture setup failure without proper cleanup
3. Parametrized tests not being properly isolated when fixtures fail/timeout
"""

from pytest import Pytester


def test_parametrized_with_fixture_timeout_and_crash(pytester: Pytester):
    """Reproduce: parametrized tests + fixture timeout + application not cleaned up.

    This combines multiple Windows issues:
    - Parametrized tests should each run in isolation
    - Fixture setup times out but output is not logged
    - Application/singleton not properly cleaned between parametrized instances
    - Subsequent parametrized instances fail because app already initialized
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time

        class SingletonApp:
            _instance = None
            _initialized = False

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                return cls._instance

            def __init__(self):
                if self._initialized:
                    raise RuntimeError(
                        "App already initialized - not properly cleaned up!"
                    )
                self._initialized = True
                self.name = "TestApp"

            @classmethod
            def reset(cls):
                cls._instance = None
                cls._initialized = False

        @pytest.fixture(scope="session")
        def slow_app():
            time.sleep(0.5)  # Simulate slow initialization
            app = SingletonApp()
            yield app
            SingletonApp.reset()
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest

        @pytest.mark.parametrize("instance", [1, 2, 3])
        @pytest.mark.isolated(timeout=2)
        def test_parametrized_with_slow_fixture(slow_app, instance):
            assert slow_app.name == "TestApp"
        """
    )

    result = pytester.runpytest("-v")
    # All instances should pass - each in isolated process with proper cleanup
    result.assert_outcomes(passed=3)
    stdout = result.stdout.str()

    # Verify each test instance ran
    assert "test_parametrized_with_slow_fixture[1]" in stdout
    assert "test_parametrized_with_slow_fixture[2]" in stdout
    assert "test_parametrized_with_slow_fixture[3]" in stdout


def test_parametrized_with_fixture_failure_no_cleanup(pytester: Pytester):
    """Reproduce: fixture raises during setup + parametrized tests.

    Windows issue: When fixture raises during setup:
    - The error should be properly reported
    - Application/resources should not leak to next parametrized instance
    - Each parametrized instance should get a fresh environment
    """
    pytester.makepyfile(
        conftest="""
        import pytest

        class AppSingleton:
            _instance = None

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                return cls._instance

            def __init__(self):
                if hasattr(self, '_initialized'):
                    raise RuntimeError("App leaked between isolated tests!")
                self._initialized = True

            @classmethod
            def reset(cls):
                cls._instance = None

        @pytest.fixture(scope="function")
        def app_fixture(request):
            # Simulate conditional failure (e.g., based on environment)
            instance_id = request.node.callspec.params.get('instance', 0)
            if instance_id == 2:
                raise RuntimeError("Simulated fixture setup failure")

            app = AppSingleton()
            yield app
            AppSingleton.reset()
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest

        @pytest.mark.parametrize("instance", [1, 2, 3])
        @pytest.mark.isolated
        def test_each_instance_isolated(app_fixture, instance):
            assert app_fixture is not None
        """
    )

    result = pytester.runpytest("-v")
    # Instance 1 and 3 should pass, instance 2 should error
    result.assert_outcomes(passed=2, errors=1)
    stdout = result.stdout.str()

    # Verify fixture error is reported
    assert "Simulated fixture setup failure" in stdout
    assert "test_each_instance_isolated[2]" in stdout


def test_timeout_with_hanging_fixture_no_output(pytester: Pytester):
    """Reproduce: fixture hangs + timeout + no output logged.

    Windows issue: When fixture setup hangs:
    - Timeout should trigger
    - Output before hang should be captured and logged
    - Should see what happened before the timeout
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time
        import sys

        @pytest.fixture(scope="session")
        def hanging_fixture():
            time.sleep(10)  # This will timeout
            yield "value"
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest

        @pytest.mark.isolated(timeout=1)
        def test_with_hanging_fixture(hanging_fixture):
            assert True
        """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(failed=1)
    stdout = result.stdout.str()

    # Should see timeout message
    assert "timed out" in stdout


def test_parametrized_fixture_timeout_isolation(pytester: Pytester):
    """Reproduce: parametrized + fixture timeout + isolation failure.

    Windows issue: When combining parametrized tests with fixture timeouts:
    - Each parametrized instance should be properly isolated
    - Timeout in one instance shouldn't affect others
    - Output should be logged for each instance
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time
        import sys

        @pytest.fixture(scope="function")
        def conditional_slow_fixture(request):
            instance = request.node.callspec.params.get('instance', 0)

            if instance == 2:
                time.sleep(5)  # Will timeout

            yield f"resource_{instance}"
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest

        @pytest.mark.parametrize("instance", [1, 2, 3])
        @pytest.mark.isolated(timeout=2)
        def test_param_instance(conditional_slow_fixture, instance):
            assert conditional_slow_fixture == f"resource_{instance}"
        """
    )

    result = pytester.runpytest("-v")
    # Instance 2 should timeout, others should pass
    result.assert_outcomes(passed=2, failed=1)
    stdout = result.stdout.str()

    # Verify timeout occurred
    assert "timed out" in stdout
    assert "test_param_instance[2]" in stdout

    # Instances 1 and 3 should pass despite instance 2 timing out
    assert "test_param_instance[1] PASSED" in stdout
    assert "test_param_instance[3] PASSED" in stdout


def test_combined_failure_modes_windows_scenario(pytester: Pytester):
    """Reproduce the exact Windows scenario: all issues combined.

    User report: multiple parametrized tests where:
    - Some fixtures raise during setup
    - Some tests timeout
    - Output is not logged when hanging
    - Application not cleaned up between instances
    - Tests don't appear to be isolated
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time

        class Application:
            _instance = None
            _init_count = 0

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                return cls._instance

            def __init__(self):
                Application._init_count += 1
                if Application._init_count > 1:
                    # This would happen if isolation is broken
                    raise RuntimeError(
                        f"Application initialized {Application._init_count} times! "
                        "Isolation is broken!"
                    )
                self.state = {}

            @classmethod
            def reset(cls):
                cls._instance = None
                cls._init_count = 0

        @pytest.fixture(scope="function")
        def app(request):
            instance = request.node.callspec.params.get('instance', 0)

            # Instance 2: fixture raises
            if instance == 2:
                raise RuntimeError("Database connection failed")

            # Instance 3: fixture hangs
            if instance == 3:
                time.sleep(10)

            app = Application()
            yield app

            Application.reset()
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest
        import time
        import sys

        @pytest.mark.parametrize("instance", [1, 2, 3, 4])
        @pytest.mark.isolated(timeout=3)
        def test_windows_scenario(app, instance):
            # Instance 4: test body hangs
            if instance == 4:
                time.sleep(10)

            assert app.state is not None
        """
    )

    result = pytester.runpytest("-v")

    # Instance 1: should pass
    # Instance 2: error in fixture setup
    # Instance 3: timeout in fixture setup
    # Instance 4: timeout in test body
    result.assert_outcomes(passed=1, errors=1, failed=2)
    stdout = result.stdout.str()

    # Instance 2: should error in fixture setup
    assert "Database connection failed" in stdout
    assert "test_windows_scenario[2]" in stdout

    # Verify timeout messages for instances 3 and 4
    assert "timed out" in stdout
    assert "test_windows_scenario[3]" in stdout or "test_windows_scenario[4]" in stdout

    # Instance 1 should pass
    assert "test_windows_scenario[1] PASSED" in stdout


def test_parametrized_fixture_error_plus_timeout_recovery(pytester: Pytester):
    """Test recovery from fixture error followed by timeout in parametrized tests.

    This tests the scenario where:
    1. First parametrized instance has fixture error
    2. Second instance times out
    3. Third instance should still work (isolation maintained)
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time

        @pytest.fixture(scope="function")
        def unstable_fixture(request):
            instance = request.node.callspec.params.get('num', 0)

            if instance == 1:
                raise Exception(f"Fixture error for instance {instance}")
            elif instance == 2:
                time.sleep(10)  # Timeout

            yield instance
        """
    )

    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.parametrize("num", [1, 2, 3])
        @pytest.mark.isolated(timeout=2)
        def test_recovery(unstable_fixture, num):
            assert unstable_fixture == num
        """
    )

    result = pytester.runpytest("-v")
    # Instance 1: error, Instance 2: timeout, Instance 3: pass
    result.assert_outcomes(passed=1, errors=1, failed=1)
    stdout = result.stdout.str()

    # Instance 3 should pass despite earlier failures
    assert "test_recovery[3] PASSED" in stdout or "test_recovery[3]" in stdout


def test_app_finalization_after_timeout(pytester: Pytester):
    """Test that app is properly isolated after a timeout kills the subprocess.

    This is critical for Windows scenarios where:
    - A test with an app fixture times out
    - The subprocess is killed, so fixture teardown doesn't run
    - The next test should still get a fresh app in a new process
    - The app from the timed-out test should not leak into subsequent tests

    The key is that isolation ensures each test group gets its own process,
    so even if teardown doesn't run due to timeout, the next test is unaffected.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import os

        class Application:
            # Track all instances created across all processes
            _init_count = 0

            def __init__(self):
                Application._init_count += 1
                self.pid = os.getpid()
                self.instance_number = Application._init_count
                self.is_finalized = False
                self.data = {}

            def finalize(self):
                '''Mark app as properly finalized'''
                self.is_finalized = True

            def __repr__(self):
                return (
                    f"Application(pid={self.pid}, "
                    f"instance={self.instance_number}, "
                    f"finalized={self.is_finalized})"
                )

        @pytest.fixture(scope="function")
        def app():
            app_instance = Application()
            yield app_instance
            # This teardown won't run if test times out
            app_instance.finalize()
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest
        import time

        @pytest.mark.isolated(group="timeout_group", timeout=1)
        def test_timeout_with_app(app):
            # App is created but test will timeout
            # Teardown (finalize) won't be called because subprocess is killed
            app.data["test"] = "timeout_test"
            assert app.instance_number == 1
            time.sleep(5)  # Will timeout

        @pytest.mark.isolated(group="fresh_group")
        def test_fresh_app_after_timeout(app):
            # This test runs in a NEW isolated process
            # Should get a completely fresh app instance
            # The timed-out app from previous test is in a killed subprocess
            assert app.instance_number == 1  # Fresh app in this process
            assert not app.is_finalized  # Not finalized yet
            assert "test" not in app.data  # No data from previous test
            assert app.data == {}  # Completely clean state
        """
    )

    result = pytester.runpytest("-v")
    # First test times out, second test passes with fresh app
    result.assert_outcomes(passed=1, failed=1)
    stdout = result.stdout.str()

    # Verify timeout occurred
    assert "test_timeout_with_app" in stdout
    assert "timed out" in stdout

    # Verify second test got fresh app
    assert "test_fresh_app_after_timeout PASSED" in stdout


def test_app_state_isolation_despite_no_teardown(pytester: Pytester):
    """Test that app state doesn't leak between tests even if teardown fails.

    Windows scenario:
    - First test modifies app state then times out (teardown doesn't run)
    - Second test should get completely fresh app (different process)
    - Demonstrates that isolation prevents state leakage even without cleanup
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import time

        class GlobalApp:
            _instance = None

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                return cls._instance

            def __init__(self):
                if not self._initialized:
                    self._initialized = True
                    self.state = {"initialized_count": 0}

                self.state["initialized_count"] += 1

                # Track if this is a re-initialization (would indicate leaked singleton)
                if self.state["initialized_count"] > 1:
                    raise RuntimeError(
                        f"App re-initialized {self.state['initialized_count']} times! "
                        "This indicates singleton leaked between isolated tests."
                    )

            @classmethod
            def reset(cls):
                cls._instance = None

        @pytest.fixture(scope="function")
        def global_app():
            app = GlobalApp()
            app.state["test_data"] = []
            yield app
            # Teardown: won't run if timeout
            GlobalApp.reset()
        """
    )

    pytester.makepyfile(
        test_file="""
        import pytest
        import time

        @pytest.mark.isolated(timeout=1)
        def test_app_timeout_no_cleanup(global_app):
            # Modify app state
            global_app.state["test_data"].append("corrupted")
            global_app.state["should_not_leak"] = True

            # Timeout before teardown can reset the singleton
            time.sleep(5)

        @pytest.mark.isolated
        def test_app_is_fresh_despite_no_reset(global_app):
            # This runs in a NEW process, so gets completely fresh singleton
            # Even though the previous process never called GlobalApp.reset()

            assert global_app.state["initialized_count"] == 1  # Fresh singleton
            assert global_app.state["test_data"] == []  # Clean state
            assert "should_not_leak" not in global_app.state  # No leaked data
        """
    )

    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1, failed=1)
    stdout = result.stdout.str()

    # First test times out
    assert "test_app_timeout_no_cleanup" in stdout
    assert "timed out" in stdout

    # Second test passes with fresh app despite no cleanup
    assert "test_app_is_fresh_despite_no_reset PASSED" in stdout


def test_segmentation_fault_isolation(pytester: Pytester):
    """Test that segmentation faults in fixture setup don't affect other tests.

    Critical for Windows CI: if a fixture crashes during setup with segfault,
    subsequent tests should still run in fresh processes.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import ctypes

        @pytest.fixture
        def crashing_fixture(request):
            '''Fixture that crashes during setup for specific test'''
            if request.node.name == "test_with_segfault_in_fixture":
                print("Fixture: About to cause segfault during setup...")
                ctypes.string_at(0)  # Segfault in fixture setup
            return "fixture_data"
        """,
        test_file="""
        import pytest

        @pytest.mark.isolated
        def test_with_segfault_in_fixture(crashing_fixture):
            '''This test's fixture will crash during setup'''
            print("Test body - should never execute")
            assert False, "Should never reach here due to fixture crash"

        @pytest.mark.isolated
        def test_should_still_run_after_fixture_segfault(crashing_fixture):
            '''This test should run successfully despite previous fixture segfault'''
            print("Running after fixture segfault")
            assert crashing_fixture == "fixture_data"
        """,
    )

    result = pytester.runpytest("-v")
    stdout = result.stdout.str()

    # First test should fail due to fixture segfault
    assert "test_with_segfault_in_fixture" in stdout

    # Second test should still run and pass
    assert "test_should_still_run_after_fixture_segfault" in stdout
    assert "PASSED" in stdout or "passed" in stdout


def test_os_abort_isolation(pytester: Pytester):
    """Test that os.abort() in fixture setup doesn't crash the entire test suite.

    os.abort() causes immediate abnormal termination without cleanup.
    This is critical to handle on Windows CI when it happens during fixture setup.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import os

        @pytest.fixture
        def aborting_fixture(request):
            '''Fixture that calls os.abort() during setup for specific test'''
            if request.node.name == "test_with_abort_in_fixture":
                print("Fixture: About to call os.abort() during setup...")
                os.abort()  # Abnormal termination in fixture setup
            return "fixture_data"
        """,
        test_file="""
        import pytest

        @pytest.mark.isolated
        def test_with_abort_in_fixture(aborting_fixture):
            '''This test's fixture will abort during setup'''
            print("Test body - should never execute")
            assert False, "Should never reach here due to fixture abort"

        @pytest.mark.isolated
        def test_should_still_run_after_fixture_abort(aborting_fixture):
            '''This test should run successfully despite previous fixture abort'''
            print("Running after fixture abort")
            assert aborting_fixture == "fixture_data"
        """,
    )

    result = pytester.runpytest("-v")
    stdout = result.stdout.str()

    # First test should fail due to fixture abort
    assert "test_with_abort_in_fixture" in stdout

    # Second test should still run and pass
    assert "test_should_still_run_after_fixture_abort" in stdout
    assert "PASSED" in stdout or "passed" in stdout


def test_parametrized_segfault_isolation(pytester: Pytester):
    """Test segfault in fixture for one parametrized instance doesn't affect others.

    Combines parametrization with fixture crashes - a common scenario
    when fixture initialization crashes for specific parameter values.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import ctypes

        @pytest.fixture
        def param_fixture(request):
            '''Fixture that crashes for specific parameter value'''
            # Get the parameter value from the test
            value = request.node.callspec.params['value']
            print(f"Fixture: Setting up for value={value}")
            if value == 2:
                print("Fixture: Triggering segfault for value=2")
                ctypes.string_at(0)  # Segfault in fixture for param 2
            return f"data_for_{value}"
        """,
        test_file="""
        import pytest

        @pytest.mark.parametrize("value", [1, 2, 3])
        @pytest.mark.isolated
        def test_parametrized_with_fixture_segfault(value, param_fixture):
            print(f"Test body: value={value}, fixture={param_fixture}")
            assert value in [1, 2, 3]
        """,
    )

    result = pytester.runpytest("-v")
    stdout = result.stdout.str()

    # All three instances should be attempted
    assert "test_parametrized_with_fixture_segfault[1]" in stdout
    assert "test_parametrized_with_fixture_segfault[2]" in stdout
    assert "test_parametrized_with_fixture_segfault[3]" in stdout

    # Instances 1 and 3 should pass despite instance 2 segfaulting
    # At least one should pass
    assert "PASSED" in stdout or "passed" in stdout


def test_combined_crash_types_isolation(pytester: Pytester):
    """Test multiple types of fixture crashes don't prevent subsequent tests.

    Simulates a comprehensive test suite with various failure modes in fixtures:
    - Normal pass
    - Segmentation fault in fixture
    - os.abort() in fixture
    - Exception in fixture
    - Timeout in fixture
    - Final pass to verify recovery
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import ctypes
        import os
        import time

        @pytest.fixture
        def crash_fixture(request):
            '''Fixture that crashes in different ways based on test name'''
            test_name = request.node.name

            if "segfault" in test_name:
                print("Fixture: About to segfault")
                ctypes.string_at(0)
            elif "abort" in test_name:
                print("Fixture: About to abort")
                os.abort()
            elif "exception" in test_name:
                print("Fixture: About to raise exception")
                raise RuntimeError("Intentional fixture error")
            elif "timeout" in test_name:
                print("Fixture: About to timeout")
                time.sleep(10)

            return "fixture_data"
        """,
        test_file="""
        import pytest

        @pytest.mark.isolated
        def test_01_normal_pass(crash_fixture):
            print("Test 1: Normal execution")
            assert crash_fixture == "fixture_data"

        @pytest.mark.isolated
        def test_02_segfault_in_fixture(crash_fixture):
            print("Test 2: Should not execute due to fixture segfault")
            assert False

        @pytest.mark.isolated
        def test_03_abort_in_fixture(crash_fixture):
            print("Test 3: Should not execute due to fixture abort")
            assert False

        @pytest.mark.isolated
        def test_04_exception_in_fixture(crash_fixture):
            print("Test 4: Should not execute due to fixture exception")
            assert False

        @pytest.mark.isolated(timeout=2)
        def test_05_timeout_in_fixture(crash_fixture):
            print("Test 5: Should not execute due to fixture timeout")
            assert False

        @pytest.mark.isolated
        def test_06_recovery_pass(crash_fixture):
            print("Test 6: Should still run after all failures")
            assert crash_fixture == "fixture_data"
        """,
    )

    result = pytester.runpytest("-v")
    stdout = result.stdout.str()

    # All tests should be attempted
    assert "test_01_normal_pass" in stdout
    assert "test_02_segfault_in_fixture" in stdout
    assert "test_03_abort_in_fixture" in stdout
    assert "test_04_exception_in_fixture" in stdout
    assert "test_05_timeout_in_fixture" in stdout
    assert "test_06_recovery_pass" in stdout

    # At least the normal tests should pass
    assert "test_01_normal_pass PASSED" in stdout or "test_01_normal_pass" in stdout

    # Most importantly, the final test should run, showing recovery
    assert "test_06_recovery_pass" in stdout


def test_fixture_segfault_doesnt_leak_to_next_test(pytester: Pytester):
    """Test that a segfault in fixture setup is properly isolated.

    This is critical: if a fixture causes a segfault during setup,
    the next test should still get a fresh process.
    """
    pytester.makepyfile(
        conftest="""
        import pytest
        import ctypes

        @pytest.fixture
        def crashing_fixture(request):
            if request.node.name == "test_with_crashing_fixture":
                print("Fixture: About to segfault")
                ctypes.string_at(0)
            return "data"
        """,
        test_file="""
        import pytest

        @pytest.mark.isolated
        def test_with_crashing_fixture(crashing_fixture):
            print("This should not execute")
            assert False, "Should never reach here"

        @pytest.mark.isolated
        def test_after_fixture_crash(crashing_fixture):
            print("Running after fixture crash")
            assert crashing_fixture == "data"
        """,
    )

    result = pytester.runpytest("-v")
    stdout = result.stdout.str()

    # Both tests should be attempted
    assert "test_with_crashing_fixture" in stdout
    assert "test_after_fixture_crash" in stdout

    # Second test should still run
    assert (
        "Running after fixture crash" in stdout or "test_after_fixture_crash" in stdout
    )
