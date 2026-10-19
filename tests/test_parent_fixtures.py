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

import pytest
from pytest import Pytester

from pytest_isolated.execution import _pytest_supports_parent_fixtures

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

    def test_setup_error_also_stops_normal_items(self, pytester: Pytester) -> None:
        """The abort from
        https://github.com/dyollb/pytest-isolated/issues/77 covers the whole
        run: a non-isolated Item outside the broken fixture's visibility must
        not cause side effects after Parent initialization failed."""
        broken = pytester.mkdir("broken")
        (broken / "conftest.py").write_text(
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
        (broken / "test_iso.py").write_text(
            """
import pytest


@pytest.mark.isolated
def test_isolated():
    assert True
"""
        )
        pytester.makepyfile(
            test_plain="""
import pathlib


def test_plain():
    pathlib.Path("plain.ran").write_text("x")
"""
        )

        result = pytester.runpytest()

        assert result.ret != 0
        assert not (pytester.path / "plain.ran").exists()

    def test_parent_fixture_skip_skips_isolated_items(self, pytester: Pytester) -> None:
        """pytest.skip() raises an outcome exception (BaseException-based);
        it must report the Items skipped, not crash the session."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def gated_setup():
    if not {CHILD_GUARD}:
        pytest.skip("service unavailable")
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
"""
        )

        result = pytester.runpytest()

        assert result.ret == 0
        assert "1 skipped" in result.stdout.str()
        assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
        assert not (pytester.path / "child.ran").exists()

    def test_parent_fixture_fail_reports_setup_error(self, pytester: Pytester) -> None:
        """pytest.fail() in a Parent fixture is a reported setup failure."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def failing_setup():
    if not {CHILD_GUARD}:
        pytest.fail("dead on arrival")
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
"""
        )

        result = pytester.runpytest()

        assert result.ret != 0
        assert "dead on arrival" in result.stdout.str() + result.stderr.str()
        assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
        assert not (pytester.path / "child.ran").exists()

    def test_parent_fixture_xfail_reports_xfailed(self, pytester: Pytester) -> None:
        """pytest.xfail() in a Parent fixture is reported, not fatal."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def xfail_setup():
    if not {CHILD_GUARD}:
        pytest.xfail("known-broken environment")
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
        assert "1 xfailed" in result.stdout.str()
        assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()

    def test_parent_fixture_reasonless_xfail_stays_xfailed(
        self, pytester: Pytester
    ) -> None:
        """pytest.xfail() allows an empty reason; the report must still carry
        the xfail classification, not collapse into a plain skip."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def xfail_setup():
    if not {CHILD_GUARD}:
        pytest.xfail()
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
        assert "1 xfailed" in result.stdout.str()
        assert "1 skipped" not in result.stdout.str()

    def test_parent_fixture_reasonless_skip_gets_fallback_reason(
        self, pytester: Pytester
    ) -> None:
        """pytest.skip() allows an empty reason; -rs summaries and JUnit
        consumers need a nonempty reason string."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def gated_setup():
    if not {CHILD_GUARD}:
        pytest.skip()
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

        result = pytester.runpytest("-rs")

        assert result.ret == 0
        assert "1 skipped" in result.stdout.str()
        assert "no reason" in result.stdout.str().lower()


class TestParentFixturePytestVersionGate:
    """The Parent fixture mechanism needs pytest>=8 fixture internals; on
    pytest 7 the run must keep the pre-#75 behavior, never abort."""

    @pytest.mark.parametrize(
        ("version", "supported"),
        [
            ("7.0.0", False),
            ("7.4.4", False),
            ("8.0.0", True),
            ("8.0.0rc1", True),
            ("9.1.1", True),
            ("2025.1", True),
            ("nightly", True),  # unparsable: assume a modern pytest
        ],
    )
    def test_gate_predicate(self, version: str, supported: bool) -> None:
        assert _pytest_supports_parent_fixtures(version) is supported


class TestParentFixtureFinalizerOutcomes:
    """Finalizer errors during the loop-end teardown drain are reported."""

    def test_finalizer_fail_reports_teardown_failure(self, pytester: Pytester) -> None:
        """teardown_exact re-raises pytest.fail() from a finalizer (and a
        BaseExceptionGroup for several); outcome exceptions are not
        Exceptions and must not escape as INTERNALERROR."""
        pytester.makeconftest(
            f"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def leaky_setup():
    if {CHILD_GUARD}:
        yield
        return

    def finalize():
        pytest.fail("teardown boom")

    yield
    finalize()
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

        assert result.ret != 0
        assert "teardown boom" in result.stdout.str() + result.stderr.str()
        assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()


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

    def test_strict_checks_effective_fixture_per_item(self, pytester: Pytester) -> None:
        """An earlier Item's function-scoped fixture must not shadow a later
        Item's session-scoped fixture of the same name: the effective
        FixtureDef is what gets checked, per Item."""
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    yield "session"
"""
        )
        # test_a collects first and overrides 'resource' with a
        # function-scoped fixture; test_b still uses the session one.
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.fixture
def resource():
    yield "function"


@pytest.mark.isolated
def test_uses_local(resource):
    assert resource == "function"
""",
            test_b="""
import pytest


@pytest.mark.isolated
def test_uses_session(resource):
    assert resource == "session"
""",
        )

        result = pytester.runpytest("--isolated-strict")

        assert result.ret != 0
        assert "once per Child" in result.stdout.str() + result.stderr.str()
