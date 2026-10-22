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

import dataclasses
import inspect
import sys

import pytest
from _pytest import runner, skipping
from _pytest.fixtures import FixtureManager, FixtureRequest
from _pytest.python import CallSpec2
from pytest import Pytester

from pytest_isolated.parent_fixtures import _pytest_supports_parent_fixtures

CHILD_GUARD = 'os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS")'
REQUIRES_PARENT_FIXTURES = pytest.mark.skipif(
    pytest.version_tuple[0] < 8,
    reason="Parent session fixtures require pytest >= 8",
)

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


# Session fixture requested only by tests that skip before fixture setup.
_SKIPPED_ONLY_CONFTEST = f"""
import os
import pathlib

import pytest


def _log(name):
    if not {CHILD_GUARD}:
        with pathlib.Path("lifecycle.log").open("a") as f:
            f.write(name + "\\n")


@pytest.fixture(scope="session")
def only_for_skipped():
    _log("setup")
    yield "value"
    _log("teardown")


def pytest_sessionfinish(session):
    _log("sessionfinish")
"""


class TestParentFixtureCollection:
    def test_collect_only_sets_up_nothing(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
def test_one():
    assert True
"""
        )

        result = pytester.runpytest("--collect-only")

        assert result.ret == 0
        assert _log_lines(pytester) == ["sessionfinish"]


@REQUIRES_PARENT_FIXTURES
class TestParentSessionFixtures:
    """The Parent sets up the session-scope autouse stack before Children."""

    @pytest.mark.parametrize("scope", ["function", "session"])
    def test_direct_parameter_overrides_session_autouse(
        self, pytester: Pytester, scope: str
    ) -> None:
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session", autouse=True)
def resource():
    pytest.fail("shadowed autouse fixture must not run")
"""
        )
        pytester.makepyfile(
            f"""
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize("resource", ["parameter"], scope={scope!r})
def test_parameter(resource):
    assert resource == "parameter"
"""
        )

        result = pytester.runpytest("--isolated-strict")

        result.assert_outcomes(passed=1)

    def test_parent_dependency_uses_item_parameter(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    pytest.fail("shadowed dependency must not run")


@pytest.fixture(scope="session", autouse=True)
def service(resource):
    assert resource == "parameter"
"""
        )
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize("resource", ["parameter"], scope="session")
def test_parameter(resource):
    assert resource == "parameter"
"""
        )

        result = pytester.runpytest("--isolated-strict")

        result.assert_outcomes(passed=1)

    def test_autouse_dependency_is_allowed_in_strict_mode(
        self, pytester: Pytester
    ) -> None:
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
def test_one():
    pass
"""
        )

        result = pytester.runpytest("--isolated-strict")

        result.assert_outcomes(passed=1)
        assert _log_lines(pytester) == [
            "helper_setup",
            "setup",
            "teardown",
            "helper_teardown",
            "sessionfinish",
        ]

    @pytest.mark.parametrize("source", ["cli", "ini", "env"])
    def test_serial_children_preserve_parent_temporary_resources(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch, source: str
    ) -> None:
        base = pytester.path / "shared-base"
        resource = base / "parent-resource.txt"
        pytester.makeconftest(
            f"""
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def service(tmp_path_factory):
    if {CHILD_GUARD}:
        yield
        return
    resource = tmp_path_factory.getbasetemp() / "parent-resource.txt"
    resource.write_text("live")
    yield
    assert resource.read_text() == "live"
    Path("parent.finalized").write_text("done")
"""
        )
        pytester.makepyfile(
            f"""
from pathlib import Path

import pytest


@pytest.mark.isolated
def test_first_child(tmp_path, cache):
    assert Path({str(resource)!r}).read_text() == "live"
    Path("first-base.txt").write_text(str(tmp_path.parent))
    cache.set("parent-fixtures/shared", "first Child")


@pytest.mark.isolated
def test_second_child(tmp_path_factory, cache):
    assert Path({str(resource)!r}).read_text() == "live"
    Path("second-base.txt").write_text(str(tmp_path_factory.getbasetemp()))
    assert cache.get("parent-fixtures/shared", None) == "first Child"


def test_plain(tmp_path, cache):
    assert Path({str(resource)!r}).read_text() == "live"
    assert cache.get("parent-fixtures/shared", None) == "first Child"
"""
        )

        if source == "cli":
            result = pytester.runpytest("--basetemp", str(base))
        else:
            option = f'--basetemp="{base}"'
            if source == "ini":
                pytester.makeini(f"[pytest]\naddopts = {option}\n")
            else:
                monkeypatch.setenv("PYTEST_ADDOPTS", option)
            # pytester's runpytest helpers inject their own CLI basetemp.
            result = pytester.run(sys.executable, "-m", "pytest")

        result.assert_outcomes(passed=3)
        assert (pytester.path / "parent.finalized").read_text() == "done"
        assert (pytester.path / "first-base.txt").read_text() != (
            pytester.path / "second-base.txt"
        ).read_text()

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

    def test_callspec_narrowed_autouse_not_set_up_in_parent(
        self, pytester: Pytester
    ) -> None:
        """parametrize(scope="function") narrows a session autouse fixture
        below session for the Item, so the Parent must leave it to the
        Children's per-Item setup. Setting it up from the Parent anchors a
        function-scope finalizer off the Parent's stack, which trips an
        assert in pytest's runner and aborts a valid suite."""
        pytester.makeconftest(
            f"""
import os
import pathlib

import pytest


def _log(name):
    if not {CHILD_GUARD}:
        with pathlib.Path("lifecycle.log").open("a") as f:
            f.write(name + "\\n")


@pytest.fixture(scope="session", autouse=True)
def narrowed():
    _log("setup")
    yield "value"
    _log("teardown")


def pytest_sessionfinish(session):
    _log("sessionfinish")
"""
        )
        pytester.makepyfile(
            test_narrow="""
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize(
    "narrowed", [1, 2], indirect=True, scope="function"
)
def test_narrow(narrowed):
    assert narrowed == "value"
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester) == ["sessionfinish"]

    def test_subdirectory_override_dependencies_warmed_per_item(
        self, pytester: Pytester
    ) -> None:
        """A subdirectory override of a session autouse fixture is a
        different fixture for those Items and must be set up -- with its
        dependencies resolved against each Item's own closure -- even when
        the base definition was already warm for other Items."""
        pytester.makeconftest(
            f"""
import os
import pathlib

import pytest


def _log(name):
    if not {CHILD_GUARD}:
        with pathlib.Path("lifecycle.log").open("a") as f:
            f.write(name + "\\n")


@pytest.fixture(scope="session")
def dep():
    _log("dep_setup")
    yield "dep"


@pytest.fixture(scope="session", autouse=True)
def base():
    _log("base_setup")
    yield "base"


def pytest_sessionfinish(session):
    _log("sessionfinish")
"""
        )
        pytester.makepyfile(
            **{
                "sub/conftest": f"""
import os
import pathlib

import pytest


def _log(name):
    if not {CHILD_GUARD}:
        with pathlib.Path("lifecycle.log").open("a") as f:
            f.write(name + "\\n")


@pytest.fixture(scope="session", autouse=True)
def base(base, dep):
    assert base == "base"
    assert dep == "dep"
    _log("sub_base_setup")
    yield "sub"
""",
                "sub/test_sub": """
import pytest


@pytest.mark.isolated
def test_sub():
    assert True
""",
            }
        )
        pytester.makepyfile(
            test_root="""
import pytest


@pytest.mark.isolated
def test_root():
    assert True
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        # the override runs in the Parent too, resolving its own-name
        # dependency to the warm base definition instead of re-running it
        assert set(_log_lines(pytester)) == {
            "base_setup",
            "sub_base_setup",
            "dep_setup",
            "sessionfinish",
        }

    @pytest.mark.parametrize("marker", ["skip", "xfail"])
    def test_pre_skipped_items_never_set_up(
        self, pytester: Pytester, marker: str
    ) -> None:
        """Skipped and xfail(run=False) Items fail out of
        pytest_runtest_setup before any fixture runs, so the Parent must
        not set their session fixtures up either."""
        mark = (
            "@pytest.mark.skip" if marker == "skip" else "@pytest.mark.xfail(run=False)"
        )
        pytester.makeconftest(_SKIPPED_ONLY_CONFTEST)
        pytester.makepyfile(
            test_never=f"""
import pytest


@pytest.mark.isolated
{mark}
def test_never_runs(only_for_skipped):
    assert only_for_skipped == "value"
""",
            test_live="""
import pytest


@pytest.mark.isolated
def test_live():
    assert True
""",
        )

        result = pytester.runpytest()

        if marker == "skip":
            result.assert_outcomes(passed=1, skipped=1)
        else:
            result.assert_outcomes(passed=1, xfailed=1)
        assert _log_lines(pytester) == ["sessionfinish"]


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

    @pytest.mark.skipif(pytest.version_tuple[0] >= 8, reason="pytest-7 fallback")
    @pytest.mark.parametrize("strict_source", ["flag", "ini"])
    def test_pytest7_warns_and_keeps_children_running(
        self, pytester: Pytester, strict_source: str
    ) -> None:
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            """
import pytest


@pytest.fixture(scope="session")
def per_child():
    return "value"


@pytest.mark.isolated
def test_one(per_child):
    assert per_child == "value"
"""
        )
        args = []
        if strict_source == "ini":
            pytester.makeini("[pytest]\nisolated_strict = true\n")
        else:
            args.append("--isolated-strict")

        result = pytester.runpytest_subprocess(*args)

        result.assert_outcomes(passed=1)
        assert _log_lines(pytester) == ["sessionfinish"]
        output = result.stdout.str() + result.stderr.str()
        assert "requires pytest>=8" in output
        assert "once per Child" not in output
        assert "INTERNALERROR" not in output


@REQUIRES_PARENT_FIXTURES
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


@REQUIRES_PARENT_FIXTURES
class TestParentFixtureDrainOnExitFirst:
    """--maxfail aborts the loop mid-pool: Parent finalizers must still
    drain before pytest_sessionfinish, while unlaunched Groups stay
    silent (xdist shutdown semantics)."""

    def test_teardown_drains_before_sessionfinish_on_maxfail(
        self, pytester: Pytester
    ) -> None:
        pytester.makeconftest(_SESSION_STACK_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated(group="a")
def test_fails():
    raise RuntimeError("boom")
""",
            test_b="""
import pytest


@pytest.mark.isolated(group="b")
def test_passes():
    assert True
""",
        )

        result = pytester.runpytest("--maxfail=1")

        assert result.ret == pytest.ExitCode.TESTS_FAILED
        # Group b never launches and reports nothing
        result.assert_outcomes(failed=1)
        assert _log_lines(pytester) == [
            "helper_setup",
            "setup",
            "teardown",
            "helper_teardown",
            "sessionfinish",
        ]


@REQUIRES_PARENT_FIXTURES
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

    @pytest.mark.parametrize("strict", [False, True])
    def test_direct_parameter_does_not_warn(
        self, pytester: Pytester, strict: bool
    ) -> None:
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    pytest.fail("shadowed session fixture must not run")
"""
        )
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize("resource", ["parameter"])
def test_parameter(resource):
    assert resource == "parameter"
"""
        )

        result = pytester.runpytest_subprocess(
            *(["--isolated-strict"] if strict else [])
        )

        result.assert_outcomes(passed=1)
        assert "once per Child" not in result.stdout.str() + result.stderr.str()

    @pytest.mark.parametrize("parameter_first", [False, True])
    @pytest.mark.parametrize("strict", [False, True])
    def test_parameter_does_not_hide_another_items_session_fixture(
        self, pytester: Pytester, parameter_first: bool, strict: bool
    ) -> None:
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    return "session"
"""
        )
        parameter = """
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize("resource", ["parameter"])
def test_parameter(resource):
    assert resource == "parameter"
"""
        session = """
import pytest


@pytest.mark.isolated
def test_session(resource):
    assert resource == "session"
"""
        pytester.makepyfile(
            test_a=parameter if parameter_first else session,
            test_b=session if parameter_first else parameter,
        )

        result = pytester.runpytest_subprocess(
            *(["--isolated-strict"] if strict else [])
        )

        output = result.stdout.str() + result.stderr.str()
        assert "once per Child" in output
        if strict:
            assert result.ret == pytest.ExitCode.USAGE_ERROR
        else:
            result.assert_outcomes(passed=2)

    def test_warns_by_default(self, pytester: Pytester) -> None:
        pytester.makepyfile(test_a=self._NONAUTOUSE_TEST)

        # subprocess mode: in-process runs leak the warning into the outer
        # session's registry before the inner run can report it
        result = pytester.runpytest_subprocess()

        assert result.ret == 0
        # routed through pytest's config-warning pipeline so it honors
        # filterwarnings, which reports it in the stdout warnings summary
        assert "once per Child" in result.stdout.str()

    def test_warning_suppressed_by_filterwarnings(self, pytester: Pytester) -> None:
        """The warning goes through pytest's warning machinery, so an
        explicit filter can suppress it."""
        pytester.makepyfile(test_a=self._NONAUTOUSE_TEST)
        pytester.makeini("[pytest]\nfilterwarnings = ignore::UserWarning\n")

        result = pytester.runpytest_subprocess()

        result.assert_outcomes(passed=1)
        assert "once per Child" not in result.stdout.str() + result.stderr.str()

    def test_filterwarnings_error_promotes_warning_to_usage_error(
        self, pytester: Pytester
    ) -> None:
        """filterwarnings=error escalates the warning to the same usage
        error --isolated-strict raises, not an INTERNALERROR."""
        pytester.makepyfile(test_a=self._NONAUTOUSE_TEST)
        pytester.makeini("[pytest]\nfilterwarnings = error::UserWarning\n")

        result = pytester.runpytest_subprocess()

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        output = result.stdout.str() + result.stderr.str()
        assert "once per Child" in output
        assert "INTERNALERROR" not in output

    def test_callspec_widened_fixture_flagged(self, pytester: Pytester) -> None:
        """parametrize(scope="session") turns a function fixture into a
        session fixture for the Item: once-per-Child trap applies."""
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.fixture
def widened():
    yield "value"


@pytest.mark.isolated
@pytest.mark.parametrize("widened", [None], indirect=True, scope="session")
def test_one(widened):
    assert widened == "value"
"""
        )

        result = pytester.runpytest_subprocess("--isolated-strict")

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        assert "once per Child" in result.stdout.str() + result.stderr.str()

    def test_callspec_narrowed_fixture_not_flagged(self, pytester: Pytester) -> None:
        """parametrize(scope="function") narrows a session fixture to
        per-function runs: the once-per-Child message does not apply."""
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.fixture(scope="session")
def narrowed():
    yield "value"


@pytest.mark.isolated
@pytest.mark.parametrize("narrowed", [1, 2], indirect=True, scope="function")
def test_one(narrowed):
    assert narrowed == "value"
"""
        )

        result = pytester.runpytest_subprocess("--isolated-strict")

        result.assert_outcomes(passed=2)
        assert "once per Child" not in result.stdout.str() + result.stderr.str()

    def test_wrapper_shadowing_hides_a_cold_session_base(
        self, pytester: Pytester
    ) -> None:
        """A function wrapper that requests its own name chains to the
        session base below it, so the cold base really does run once per
        Child and must be flagged."""
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    yield "session"
"""
        )
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.fixture
def resource(resource):
    return resource + "/wrapper"


@pytest.mark.isolated
def test_wrapped(resource):
    assert resource == "session/wrapper"
"""
        )

        result = pytester.runpytest_subprocess("--isolated-strict")

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        assert "once per Child" in result.stdout.str() + result.stderr.str()

    def test_unused_shadowed_override_not_flagged(self, pytester: Pytester) -> None:
        """An override that never requests its own name leaves the session
        definition below it dead for these Items: nothing runs per Child."""
        pytester.makeconftest(
            """
import pytest


@pytest.fixture(scope="session")
def resource():
    yield "session"
"""
        )
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.fixture
def resource():
    return "shadow"


@pytest.mark.isolated
def test_shadow(resource):
    assert resource == "shadow"
"""
        )

        result = pytester.runpytest_subprocess("--isolated-strict")

        result.assert_outcomes(passed=1)
        assert "once per Child" not in result.stdout.str() + result.stderr.str()

    @pytest.mark.parametrize("marker", ["skip", "xfail"])
    def test_pre_skipped_items_do_not_trigger_strict_error(
        self, pytester: Pytester, marker: str
    ) -> None:
        """Items pytest skips before fixture setup never run their fixtures
        anywhere, so their session fixtures are not a once-per-Child trap."""
        mark = (
            "@pytest.mark.skip" if marker == "skip" else "@pytest.mark.xfail(run=False)"
        )
        pytester.makepyfile(
            test_never=f"""
import pytest


@pytest.fixture(scope="session")
def only_for_skipped():
    yield "value"


@pytest.mark.isolated
{mark}
def test_never_runs(only_for_skipped):
    assert only_for_skipped == "value"
""",
            test_live="""
import pytest


@pytest.mark.isolated
def test_live():
    assert True
""",
        )

        result = pytester.runpytest("--isolated-strict")

        assert result.ret == 0
        assert "once per Child" not in result.stdout.str() + result.stderr.str()
        assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()

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


@REQUIRES_PARENT_FIXTURES
class TestPytestInternalsPresence:
    """Pin the private pytest surface ``parent_fixtures`` resolves at
    runtime. The module degrades to the pre-#75 behavior when a symbol
    moves, and the version gate only covers major versions, so these
    checks make internals drift fail the suite on a pytest upgrade
    instead of silently disabling the feature in the field."""

    def test_fixture_manager_lookups(self) -> None:
        assert hasattr(FixtureManager, "getfixturedefs")
        assert hasattr(FixtureManager, "_getautousenames")

    def test_skipping_helpers(self) -> None:
        assert hasattr(skipping, "evaluate_skip_marks")
        assert hasattr(skipping, "evaluate_xfail_marks")

    def test_fixture_request_constructor(self) -> None:
        parameters = inspect.signature(FixtureRequest.__init__).parameters
        assert {
            "pyfuncitem",
            "fixturename",
            "arg2fixturedefs",
            "fixture_defs",
            "_ispytest",
        } <= set(parameters)

    def test_callspec_scope_override_field(self) -> None:
        assert "_arg2scope" in {field.name for field in dataclasses.fields(CallSpec2)}

    def test_setup_state_rejects_off_stack_finalizers(self) -> None:
        """The effective-scope filter exists because SetupState asserts on
        finalizers whose node is off the stack; if that assert ever goes
        away, the filter's rationale needs revisiting."""
        assert "assert" in inspect.getsource(runner.SetupState.addfinalizer)
