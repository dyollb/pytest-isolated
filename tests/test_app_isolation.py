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
    reason="This test is expected to segfault. "
    "If properly isolated, then it will be a FAILURE otherwise it will be an ERROR."
)
def test_segfault_to_check_isolation():
    """This test is just to check the isolation of tests.

    - With isolation
        - this test fails with `Fatal Python error: Segmentation fault`
    - Without it:
        - pytest crashes
    """
    import ctypes  # noqa: PLC0415

    ctypes.string_at(0)
