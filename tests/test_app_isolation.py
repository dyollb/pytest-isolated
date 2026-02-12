import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.mark.isolated
def test_application1(application) -> None:
    """Test that application singleton works in isolated process.

    Modifies the singleton state to verify isolation from other tests.
    """
    assert application is not None
    assert application.ApplicationName == "TestApplication"

    # Check that state hasn't been modified by another test
    assert "test1" not in application.state, "State should be clean in isolated process"

    # Modify the singleton state
    application.state["test1"] = "modified_by_test1"


@pytest.mark.isolated
def test_application2(application) -> None:
    """Test that different group gets a fresh singleton in its own process.

    Verifies that the singleton state has NOT been modified by test_application1,
    proving that each isolated group runs in its own process.
    """
    assert application is not None
    assert application.ApplicationName == "TestApplication"

    # Check that test1's modification is NOT present - proves isolation worked
    assert "test1" not in application.state, (
        "State should be clean - test1's modifications should not be visible "
        "because this test runs in a separate isolated process"
    )

    # Modify with our own marker
    application.state["test2"] = "modified_by_test2"


@pytest.mark.isolated
@pytest.mark.xfail(
    reason="""A simple failing test to check isolation handling of failures."""
)
def test_failing():
    assert False, "Intentional failure to test isolation handling"  # noqa: B011


@pytest.mark.isolated
@pytest.mark.xfail(
    reason="This test is expected to crash. "
    "If properly isolated, then it will be a FAILURE otherwise it will be an ERROR."
)
def test_crash_to_check_isolation():
    """This test is just to check the isolation of tests.

    - With isolation
        - this test fails with crash message
            - SIGABRT on Unix
            - abnormal termination on Windows)
    - Without it:
        - pytest crashes
    """
    import os  # noqa: PLC0415

    os.abort()


# Shared directory for isolation testing - hardcoded so all processes use the directory
ISOLATION_CHECK_DIR = Path(tempfile.gettempdir()) / "pytest_isolated_test_check"

PARAMETRIZE_INSTANCES = [1, 2, 3, 4]


@pytest.mark.parametrize("instance", PARAMETRIZE_INSTANCES)
@pytest.mark.isolated
def test_parametrized_isolation(instance: int):
    """Verify each parametrized test runs in a separate process.

    Each instance writes its PID to a file in a shared directory.
    The verification happens in test_verify_parametrized_isolation below,
    which runs AFTER all parametrized instances complete (not isolated,
    so it can access all PID files from all subprocesses).
    """
    # Ensure directory exists
    ISOLATION_CHECK_DIR.mkdir(exist_ok=True)

    pid = os.getpid()
    marker_file = ISOLATION_CHECK_DIR / f"pid_{instance}.txt"

    # Write our PID to a file for post-test verification
    marker_file.write_text(str(pid))

    # Sanity check that we can write to the shared directory
    assert marker_file.exists(), f"Failed to create marker file for instance {instance}"


def test_verify_parametrized_isolation():
    """Verify that all parametrized instances ran in separate processes.

    This test is NOT isolated, so it runs in the parent process and can
    access all PID files written by the isolated parametrized tests above.

    By running after test_parametrized_isolation (alphabetically), this
    verifies that all instances completed and ran in unique processes.
    """
    # Ensure directory exists
    ISOLATION_CHECK_DIR.mkdir(exist_ok=True)

    pid_files = sorted(ISOLATION_CHECK_DIR.glob("pid_*.txt"))

    # Verify all expected instances wrote PID files
    expected_count = len(PARAMETRIZE_INSTANCES)
    assert len(pid_files) == expected_count, (
        f"Expected {expected_count} PID files but found {len(pid_files)}. "
        f"Files: {[f.name for f in pid_files]}"
    )

    # Verify all PIDs are unique (proves process isolation)
    pids = [int(f.read_text()) for f in pid_files]
    unique_pids = set(pids)
    assert len(pids) == len(unique_pids), (
        f"Process isolation violated! Found {len(pids)} instances but only "
        f"{len(unique_pids)} unique PIDs. PIDs: {pids}"
    )

    # Cleanup after successful verification
    shutil.rmtree(ISOLATION_CHECK_DIR, ignore_errors=True)
