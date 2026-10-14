"""Pure classification of one isolated group's subprocess outcome."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple, Protocol, TypeAlias

__all__ = [
    "Clean",
    "CollectionCrash",
    "Member",
    "Outcome",
    "PartialCrash",
    "TimedOut",
    "XfailCrash",
    "classify",
]


class Member(NamedTuple):
    """A group's test identity and its expected-failure reason, if any."""

    nodeid: str
    xfail_reason: str | None


class Clean(NamedTuple):
    """No child failure requiring synthetic reports."""


class TimedOut(NamedTuple):
    """The child exceeded its timeout."""


class XfailCrash(NamedTuple):
    """An all-xfail group crashed, as expected."""

    returncode: int


class CollectionCrash(NamedTuple):
    """The child exited without producing per-test records."""

    returncode: int


class PartialCrash(NamedTuple):
    """The child crashed during a test after producing some records."""

    returncode: int
    crashed: tuple[str, ...]
    not_run: tuple[str, ...]


Outcome: TypeAlias = Clean | XfailCrash | TimedOut | CollectionCrash | PartialCrash
_Records: TypeAlias = Mapping[str, Mapping[str, Mapping[str, object]]]


class _SubprocessResult(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def timed_out(self) -> bool: ...


def classify(
    result: _SubprocessResult,
    records: _Records,
    members: Sequence[Member],
) -> Outcome:
    """Classify a child's result using only its process and report values."""
    # Keep this order: timed-out subprocesses use returncode -1, which makes an
    # all-xfail group with partial records an XfailCrash rather than TimedOut.
    if (
        result.returncode < 0
        and records
        and all(member.xfail_reason is not None for member in members)
    ):
        return XfailCrash(result.returncode)

    if result.timed_out:
        return TimedOut()

    if not records:
        return CollectionCrash(result.returncode)

    crashed = tuple(
        member.nodeid
        for member in members
        if (node_records := records.get(member.nodeid))
        and "call" not in node_records
        and node_records.get("setup", {}).get("outcome") == "passed"
    )
    if crashed:
        not_run = tuple(
            member.nodeid for member in members if not records.get(member.nodeid)
        )
        return PartialCrash(result.returncode, crashed, not_run)

    return Clean()
