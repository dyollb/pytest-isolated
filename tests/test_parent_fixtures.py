"""Tests for Parent-side session fixture setup.

pytest-isolated replaces pytest's runtestloop, which is what normally
instantiates the session fixture stack in-process. When every collected test
is Isolated, no Item ever runs in the Parent, so session-scoped fixtures
never ran (https://github.com/dyollb/pytest-isolated/issues/75); the Parent
must set them up before the first Child launches and tear them down at loop
end. Spec: https://github.com/dyollb/pytest-isolated/issues/77.

Evidence is marker files written by fixtures guarded on the Child marker
environment variable, the issue's own reproducer idiom.
"""

from __future__ import annotations

from pytest import Pytester

CHILD_GUARD = 'os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS")'

# Parent-side session autouse fixture with a dependency; each body part
# appends a line to the Parent lifecycle log when it runs in the Parent.
_SESSION_STACK_CONFTEST = f"""
import os
import pathlib

import pytest


def _log(name):
    if not {CHILD_GUARD}:
        with pathlib.Path("lifecycle.log").open("a") as f:
            f.write(name + "\\n")


@pytest.fixture(scope="session")
def helper_session():
    _log("helper_setup")
    yield "helper"
    _log("helper_teardown")


@pytest.fixture(scope="session", autouse=True)
def autouse_with_dep(helper_session):
    assert helper_session == "helper"
    _log("setup")
    yield
    _log("teardown")


def pytest_sessionfinish(session):
    _log("sessionfinish")
"""


def _log_lines(pytester: Pytester) -> list[str]:
    log = pytester.path / "lifecycle.log"
    return log.read_text().splitlines() if log.exists() else []


class TestParentSessionFixtures:
    """The Parent sets up the session-scope autouse stack before Children."""

    def test_all_isolated_session_autouse_runs_in_parent(
        self, pytester: Pytester
    ) -> None:
        """The reproducer from
        https://github.com/dyollb/pytest-isolated/issues/75: with every test
        Isolated, the session autouse fixture never ran in the Parent."""
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated
def test_one():
    assert True
"""
        )

        result = pytester.runpytest()

        assert result.ret == 0
        assert _log_lines(pytester) == [
            "helper_setup",
            "setup",
            "teardown",
            "helper_teardown",
            "sessionfinish",
        ]

    def test_parent_teardown_precedes_sessionfinish_hook(
        self, pytester: Pytester
    ) -> None:
        """Stock pytest tears the stack down inside the loop (the last Item's
        teardown drains it); the Parent must not defer teardown past
        conftest pytest_sessionfinish hooks."""
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated
def test_one():
    assert True
"""
        )

        result = pytester.runpytest()

        assert result.ret == 0
        lines = _log_lines(pytester)
        assert lines.index("teardown") < lines.index("sessionfinish")

    def test_mixed_suite_parent_setup_runs_once(self, pytester: Pytester) -> None:
        """A leftover non-isolated Item goes through the in-process protocol;
        its setup must reuse the Parent's cached fixture, not rerun it."""
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated
def test_one():
    assert True


def test_plain():
    assert True
"""
        )

        result = pytester.runpytest()

        assert result.ret == 0
        lines = _log_lines(pytester)
        assert lines.count("setup") == 1
        assert lines.count("helper_setup") == 1

    def test_module_scope_autouse_not_run_in_parent(self, pytester: Pytester) -> None:
        """Narrow-scope autouse fixtures belong to the Child that owns the
        items; the Parent takes session scope only."""
        pytester.makeconftest(
            f"""
import os
import pathlib

import pytest


@pytest.fixture(scope="module", autouse=True)
def module_fixture():
    if not {CHILD_GUARD}:
        pathlib.Path("module_in_parent.txt").write_text("x")
    yield
"""
        )
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated
def test_one():
    assert True
"""
        )

        result = pytester.runpytest()

        assert result.ret == 0
        assert not (pytester.path / "module_in_parent.txt").exists()

    def test_collect_only_sets_up_nothing(self, pytester: Pytester) -> None:
        """The Parent session setup must stay behind the collect-only
        short-circuit
        (https://github.com/dyollb/pytest-isolated/issues/76)."""
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated
def test_one():
    assert True
"""
        )

        result = pytester.runpytest("--collect-only")

        assert result.ret == 0
        # the conftest hook itself runs (hooks are not fixtures), but the
        # fixture stack must stay untouched: no setup/teardown lines.
        assert _log_lines(pytester) == ["sessionfinish"]

    def test_parent_session_fixture_error_aborts_before_children(
        self, pytester: Pytester
    ) -> None:
        """Setup failure in the Parent stack aborts the run: Children must
        not launch into an environment that failed to initialize."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def broken_setup():
    if not {CHILD_GUARD}:
        msg = "parent session setup failed"
        raise RuntimeError(msg)
    yield
"""
        )
        pytester.makepyfile(
            test_a="""
import pathlib

import pytest


@pytest.mark.isolated
def test_one():
    pathlib.Path("child.ran").write_text("x")
""",
        )

        result = pytester.runpytest()

        assert result.ret != 0
        assert "broken_setup" in result.stdout.str() + result.stderr.str()
        assert not (pytester.path / "child.ran").exists()


class TestParentSessionFixtureStrictness:
    """Non-autouse session fixtures requested by Isolated tests run once per
    Child; the strict knob surfaces that instead of leaving it silent."""

    _NONAUTOUSE_TEST = """
import pytest


@pytest.fixture(scope="session")
def per_child():
    yield "value"


@pytest.mark.isolated
def test_one(per_child):
    assert per_child == "value"
"""

    def test_warns_by_default(self, pytester: Pytester) -> None:
        pytester.makepyfile(test_a=self._NONAUTOUSE_TEST)

        # subprocess mode: in-process runs leak the warning into the outer
        # session's registry before the inner run can report it
        result = pytester.runpytest_subprocess()

        assert result.ret == 0
        # session-level warnings bypass per-item capture: they land on stderr
        assert "once per Child" in result.stderr.str()

    def test_strict_flag_promotes_warning_to_error(self, pytester: Pytester) -> None:
        pytester.makepyfile(test_a=self._NONAUTOUSE_TEST)

        result = pytester.runpytest("--isolated-strict")

        assert result.ret != 0
        assert "once per Child" in result.stdout.str() + result.stderr.str()
