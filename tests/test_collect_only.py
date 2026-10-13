"""Regression tests for #53: --collect-only must not execute tests.

pytest-isolated replaces pytest's runtestloop; the custom loop must keep
stock pytest's collect-only short-circuit, or collection runs tests.
--collect-only is Category B (parent-handled), so the child never receives
it: the guard has to stop the parent's loop before any group launches.

Evidence of execution is a side-effect file written by each test, the
reproducer from the issue.
"""

from __future__ import annotations

import pytest
from pytest import Pytester


def _mixed_file(pytester: Pytester) -> None:
    """A file with one plain and one isolated test, each leaving a marker."""
    pytester.makepyfile(
        test_mixed="""
import pathlib

import pytest


def test_plain():
    pathlib.Path("plain.ran").write_text("x")
    assert True


@pytest.mark.isolated
def test_isolated():
    pathlib.Path("isolated.ran").write_text("x")
    assert True
"""
    )


def _ran_markers(pytester: Pytester) -> list[str]:
    return sorted(
        name
        for name in ("plain.ran", "isolated.ran")
        if (pytester.path / name).exists()
    )


@pytest.mark.parametrize("flag", ["--collect-only", "--co"])
def test_collect_only_lists_but_runs_nothing(pytester: Pytester, flag: str) -> None:
    """The #53 reproducer: with an isolated test present, collection ran both
    tests (the child executed because --collect-only is never forwarded, and
    the plain one ran in the loop's in-process tail)."""
    _mixed_file(pytester)

    result = pytester.runpytest(flag)

    assert result.ret == 0
    assert _ran_markers(pytester) == []
    # collection still listed both tests
    assert "test_plain" in result.stdout.str()
    assert "test_isolated" in result.stdout.str()


def test_isolated_flag_collect_only_runs_nothing(pytester: Pytester) -> None:
    """--isolated makes every test isolated, so groups are non-empty: the same
    guard must cover a collect-only session launched via the global flag."""
    pytester.makepyfile(
        test_all="""
import pathlib


def test_one():
    pathlib.Path("one.ran").write_text("x")
    assert True
"""
    )

    result = pytester.runpytest("--isolated", "--collect-only")

    assert result.ret == 0
    assert not (pytester.path / "one.ran").exists()


def test_collect_only_with_no_isolation_runs_nothing(pytester: Pytester) -> None:
    """Bypass mode already delegated to the stock loop's guard; pinned so a
    future loop change cannot silently break it."""
    _mixed_file(pytester)

    result = pytester.runpytest("--collect-only", "--no-isolation")

    assert result.ret == 0
    assert _ran_markers(pytester) == []
