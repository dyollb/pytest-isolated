"""Plain-value tests for child outcome classification."""

from __future__ import annotations

from pytest_isolated.execution import SubprocessResult
from pytest_isolated.outcome import (
    Clean,
    CollectionCrash,
    Member,
    PartialCrash,
    TimedOut,
    XfailCrash,
    classify,
)


def _result(returncode: int = 0, *, timed_out: bool = False) -> SubprocessResult:
    return SubprocessResult(returncode, b"", b"", timed_out)


def _complete(outcome: str = "passed") -> dict[str, dict[str, str]]:
    return {
        "setup": {"outcome": "passed"},
        "call": {"outcome": outcome},
        "teardown": {"outcome": "passed"},
    }


def test_clean_result_with_complete_records() -> None:
    members = [Member("test.py::test_ok", None)]
    records = {"test.py::test_ok": _complete()}

    assert classify(_result(), records, members) == Clean()


def test_member_without_records_is_clean_when_other_records_exist() -> None:
    assert (
        classify(
            _result(),
            {"test.py::test_survived": _complete()},
            [
                Member("test.py::test_survived", None),
                Member("test.py::test_missing", None),
            ],
        )
        == Clean()
    )


def test_setup_passed_without_call_is_a_partial_crash() -> None:
    members = [Member("test.py::test_crashed", None)]
    records = {"test.py::test_crashed": {"setup": {"outcome": "passed"}}}

    assert classify(_result(-9), records, members) == PartialCrash(
        -9, ("test.py::test_crashed",), ()
    )


def test_nonpassed_setup_and_existing_call_are_not_crashes() -> None:
    for setup_outcome in ("failed", "skipped"):
        records = {
            "test.py::test_setup": {"setup": {"outcome": setup_outcome}},
        }
        assert (
            classify(_result(-9), records, [Member("test.py::test_setup", None)])
            == Clean()
        )

    assert (
        classify(
            _result(-9),
            {"test.py::test_call": {"setup": {"outcome": "passed"}, "call": {}}},
            [Member("test.py::test_call", None)],
        )
        == Clean()
    )


def test_partial_crash_partitions_crashed_not_run_and_surviving_members() -> None:
    members = [
        Member("test.py::test_survived", None),
        Member("test.py::test_crashed", None),
        Member("test.py::test_not_run", None),
        Member("test.py::test_empty_record_map", None),
    ]
    records = {
        "test.py::test_crashed": {"setup": {"outcome": "passed"}},
        "test.py::test_survived": _complete(),
        "test.py::test_empty_record_map": {},
    }

    assert classify(_result(-11), records, members) == PartialCrash(
        -11,
        ("test.py::test_crashed",),
        ("test.py::test_not_run", "test.py::test_empty_record_map"),
    )


def test_not_run_members_are_only_reported_when_a_crash_was_detected() -> None:
    members = [
        Member("test.py::test_setup_failed", None),
        Member("test.py::test_missing", None),
    ]
    records = {"test.py::test_setup_failed": {"setup": {"outcome": "failed"}}}

    assert classify(_result(-9), records, members) == Clean()


def test_partial_crash_nodeids_follow_member_order_not_record_order() -> None:
    members = [
        Member("test.py::test_first_crash", None),
        Member("test.py::test_second_crash", None),
    ]
    records = {
        "test.py::test_second_crash": {"setup": {"outcome": "passed"}},
        "test.py::test_first_crash": {"setup": {"outcome": "passed"}},
    }

    assert classify(_result(-9), records, members) == PartialCrash(
        -9,
        ("test.py::test_first_crash", "test.py::test_second_crash"),
        (),
    )


def test_all_xfail_group_with_negative_returncode_is_xfail_crash() -> None:
    members = [
        Member("test.py::test_one", "expected signal"),
        Member("test.py::test_two", "expected signal"),
    ]
    records = {"test.py::test_one": {"setup": {"outcome": "passed"}}}

    assert classify(_result(-6), records, members) == XfailCrash(-6)


def test_positive_returncode_does_not_make_xfail_group_an_xfail_crash() -> None:
    members = [Member("test.py::test_xfail", "expected failure")]
    records = {"test.py::test_xfail": _complete()}

    assert classify(_result(1), records, members) == Clean()


def test_timeout_with_records_is_timed_out() -> None:
    members = [Member("test.py::test_running", None)]
    records = {"test.py::test_running": {"setup": {"outcome": "passed"}}}

    assert classify(_result(-1, timed_out=True), records, members) == TimedOut()


def test_xfail_crash_precedes_timeout_for_synthetic_negative_returncode() -> None:
    members = [Member("test.py::test_xfail", "expected failure")]
    records = {"test.py::test_xfail": {"setup": {"outcome": "passed"}}}

    assert classify(_result(-1, timed_out=True), records, members) == XfailCrash(-1)


def test_timeout_precedes_collection_crash_when_there_are_no_records() -> None:
    assert classify(_result(-1, timed_out=True), {}, []) == TimedOut()


def test_empty_records_are_collection_crashes_for_both_returncode_signs() -> None:
    for returncode in (-9, 1):
        assert classify(_result(returncode), {}, []) == CollectionCrash(returncode)
