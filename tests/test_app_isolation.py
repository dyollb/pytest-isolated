import os
import shutil
from collections.abc import Iterator
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


@pytest.fixture(scope="module")
def module_shared_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Fixture to provide a shared directory for tests to write marker files.

    This allows tests to check for process isolation by writing their PID to a file
    and then verifying that all PIDs are unique across test instances.
    """
    shared_dir = tmp_path_factory.mktemp("isolation_check_dir", numbered=False)
    yield shared_dir
    shutil.rmtree(shared_dir, ignore_errors=True)  # Clean up after tests are done


@pytest.mark.isolated
@pytest.mark.parametrize("instance", [1, 2, 3, 4])
def test_parametrized_isolation(instance: int, module_shared_dir: Path):
    """Verify each parametrized test runs in a separate process.

    Uses a shared temp directory to track process IDs across isolated processes.
    If tests run in separate processes, each should see a unique PID.
    If they couple (same process), PIDs would repeat.

    Follows from https://git.speag.com/simphony/Simphony/-/merge_requests/4086#note_263531
    """
    pid = os.getpid()
    marker_file = module_shared_dir / f"pid_{instance}.txt"

    # Write our PID to a file
    marker_file.write_text(str(pid))

    # Verify the file was created (sanity check)
    assert marker_file.exists(), f"Failed to create marker file for instance {instance}"  # nosec

    # Collect all PIDs from completed test instances
    pids = []
    for i in range(1, 5):
        pid_file = module_shared_dir / f"pid_{i}.txt"
        if pid_file.exists():
            pids.append(int(pid_file.read_text()))

    # Verify all collected PIDs are unique - proves process isolation
    assert len(pids) == len(set(pids)), f"PIDs not unique! Found duplicates: {pids}"  # nosec
    assert pid in pids, f"Current PID {pid} not in collected PIDs {pids}"  # nosec
