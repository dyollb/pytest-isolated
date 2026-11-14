"""Tests for Shared Fixtures (https://github.com/dyollb/pytest-isolated/issues/75).

By default session fixtures run per process, as in pytest-xdist: every Child
runs what its tests need and the Parent runs nothing for Isolated Items. A
fixture opted in with ``@pytest_isolated.shared`` or the
``isolated_shared_fixtures`` ini runs once in the Parent; Children receive its
JSON value and never run the body.

Evidence is a lifecycle log in which every line says which process wrote it,
so no fixture needs a ``PYTEST_RUNNING_IN_SUBPROCESS`` guard.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from _pytest import skipping
from _pytest.fixtures import FixtureRequest, FuncFixtureInfo
from _pytest.runner import SetupState
from pytest import Pytester

from pytest_isolated import shared
from pytest_isolated.launch import plan_launch
from pytest_isolated.shared_fixtures import _pytest_supports_parent_fixtures

REQUIRES_PARENT_FIXTURES = pytest.mark.skipif(
    pytest.version_tuple[0] < 8,
    reason="Shared Fixtures require pytest >= 8",
)

_LOG_HELPER = """
import os
import pathlib

import pytest
import pytest_isolated


def _log(name):
    who = "child" if os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS") else "parent"
    with pathlib.Path(__file__).with_name("lifecycle.log").open("a") as f:
        f.write(f"{who}:{name}\\n")


def pytest_sessionfinish(session):
    _log("sessionfinish")
"""

_SHARED_CONFTEST = (
    _LOG_HELPER
    + """

@pytest.fixture(scope="session")
def expensive_dep():
    _log("dep")
    return 41


@pytest.fixture(scope="session")
@pytest_isolated.shared
def server(expensive_dep):
    _log("setup")
    yield {"url": f"http://localhost:{expensive_dep + 1}"}
    _log("teardown")
"""
)

_TWO_GROUPS = """
import pytest


@pytest.mark.isolated(group="a")
def test_a(server):
    assert server == {"url": "http://localhost:42"}


@pytest.mark.isolated(group="b")
def test_b(server):
    assert server["url"].endswith(":42")
"""

_TWO_PLAIN_GROUPS = """
import pytest


@pytest.mark.isolated(group="a")
def test_a():
    pass


@pytest.mark.isolated(group="b")
def test_b():
    pass
"""


def _log_lines(pytester: Pytester) -> list[str]:
    log = pytester.path / "lifecycle.log"
    return log.read_text().splitlines() if log.exists() else []


def _output(result: pytest.RunResult) -> str:
    return result.stdout.str() + result.stderr.str()


class TestDefaultPerProcess:
    """Without opt-in, session fixtures run per process, with a warning."""

    @pytest.mark.parametrize("autouse", [True, False])
    @pytest.mark.parametrize("mixed", [True, False])
    def test_session_fixture_warns_and_runs_per_process(
        self, pytester: Pytester, autouse: bool, mixed: bool
    ) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session", autouse=AUTOUSE)
def per_process():
    _log("setup")
    yield
    _log("teardown")
""".replace("AUTOUSE", str(autouse))
        )
        tests = (
            _TWO_PLAIN_GROUPS
            if autouse
            else _TWO_PLAIN_GROUPS.replace("():", "(per_process):")
        )
        if mixed:
            tests += "\ndef test_plain(per_process): pass\n"
        pytester.makepyfile(tests)

        result = pytester.runpytest()

        result.assert_outcomes(passed=3 if mixed else 2)
        lines = _log_lines(pytester)
        assert lines.count("child:setup") == 2
        assert lines.count("child:teardown") == 2
        assert lines.count("parent:setup") == int(mixed)
        assert lines.count("parent:teardown") == int(mixed)
        output = _output(result)
        assert output.count("UserWarning: pytest-isolated: session fixtures") == 1
        assert "::per_process" in output
        assert "@pytest_isolated.shared" in output
        assert "pytest_sessionstart/pytest_sessionfinish" in output

    @pytest.mark.parametrize(
        "options", [("--collect-only",), ("--no-isolation",), ("-m", "not isolated")]
    )
    def test_no_warning_without_isolated_execution(
        self, pytester: Pytester, options: tuple[str, ...]
    ) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session", autouse=True)
def per_process():
    _log("setup")
    yield
    _log("teardown")
"""
        )
        pytester.makepyfile(_TWO_PLAIN_GROUPS + "\ndef test_plain(): pass\n")

        result = pytester.runpytest(*options)

        assert result.ret == 0
        assert "UserWarning" not in _output(result)

    @pytest.mark.parametrize("mark", ["skip", "xfail(run=False)"])
    def test_no_warning_for_pre_skipped_items(
        self, pytester: Pytester, mark: str
    ) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session", autouse=True)
def per_process():
    _log("setup")
"""
        )
        pytester.makepyfile(
            _TWO_PLAIN_GROUPS.replace(
                "@pytest.mark.isolated",
                f"@pytest.mark.{mark}\n@pytest.mark.isolated",
            )
        )

        result = pytester.runpytest()

        assert result.ret == 0
        assert "UserWarning" not in _output(result)
        assert not [line for line in _log_lines(pytester) if line.endswith(":setup")]

    def test_collect_only_sets_up_nothing(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest("--collect-only")

        assert result.ret == 0
        assert _log_lines(pytester) == ["parent:sessionfinish"]

    def test_warns_for_session_dependency(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session")
def per_process():
    _log("setup")


@pytest.fixture
def resource(per_process):
    pass
"""
        )
        pytester.makepyfile(_TWO_PLAIN_GROUPS.replace("():", "(resource):"))

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        assert "::per_process" in _output(result)
        assert _log_lines(pytester).count("child:setup") == 2

    def test_no_warning_for_parent_only_fixture(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session")
def parent_only():
    _log("setup")
"""
        )
        pytester.makepyfile(_TWO_PLAIN_GROUPS + "\ndef test_plain(parent_only): pass\n")

        result = pytester.runpytest()

        result.assert_outcomes(passed=3)
        assert "UserWarning" not in _output(result)
        assert _log_lines(pytester).count("parent:setup") == 1
        assert "child:setup" not in _log_lines(pytester)


@REQUIRES_PARENT_FIXTURES
class TestSharedFixtures:
    @pytest.mark.parametrize("numprocesses", ["1", "2"])
    def test_runs_once_in_parent_and_children_get_value(
        self, pytester: Pytester, numprocesses: str
    ) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest(f"--isolated-numprocesses={numprocesses}")

        result.assert_outcomes(passed=2)
        lines = _log_lines(pytester)
        # Neither the body nor its dependency runs in a Child.
        assert [line for line in lines if line.startswith("parent:")] == [
            "parent:dep",
            "parent:setup",
            "parent:teardown",
            "parent:sessionfinish",
        ]
        assert sorted(line for line in lines if line.startswith("child:")) == [
            "child:sessionfinish",
            "child:sessionfinish",
        ]
        assert "UserWarning" not in _output(result)

    def test_warns_when_shared_dependency_is_also_requested_directly(
        self, pytester: Pytester
    ) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(
            _TWO_GROUPS.replace("test_b(server)", "test_b(server, expensive_dep)")
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        output = _output(result)
        assert output.count("UserWarning: pytest-isolated: session fixtures") == 1
        assert "::expensive_dep" in output
        assert _log_lines(pytester).count("child:dep") == 1

    def test_autouse_shared_with_none_value(self, pytester: Pytester) -> None:
        """The issue #75 shape: start a service for the whole run."""
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session", autouse=True)
@pytest_isolated.shared
def service():
    _log("setup")
    yield
    _log("teardown")
"""
        )
        pytester.makepyfile(_TWO_PLAIN_GROUPS)

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        lines = _log_lines(pytester)
        assert lines.count("parent:setup") == 1
        assert "child:setup" not in lines
        assert lines.index("parent:teardown") < lines.index("parent:sessionfinish")
        assert "UserWarning" not in _output(result)

    def test_decorator_order_does_not_matter(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest_isolated.shared
@pytest.fixture(scope="session")
def server():
    _log("setup")
    return {"url": "http://localhost:42"}
"""
        )
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester).count("parent:setup") == 1
        assert "child:setup" not in _log_lines(pytester)

    def test_ini_opt_in(self, pytester: Pytester) -> None:
        pytester.makeini("[pytest]\nisolated_shared_fixtures = server\n")
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session")
def server():
    _log("setup")
    return {"url": "http://localhost:42"}
"""
        )
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest()

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester).count("parent:setup") == 1
        assert "child:setup" not in _log_lines(pytester)

    def test_mixed_suite_sets_up_once(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(
            _TWO_GROUPS
            + """

def test_plain(server):
    assert server["url"].endswith(":42")
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=3)
        lines = _log_lines(pytester)
        assert lines.count("parent:setup") == 1
        assert lines.count("parent:teardown") == 1
        assert lines.index("parent:teardown") < lines.index("parent:sessionfinish")

    def test_no_isolation_runs_in_process_once(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest("--no-isolation")

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester) == [
            "parent:dep",
            "parent:setup",
            "parent:teardown",
            "parent:sessionfinish",
        ]

    def test_child_without_payload_runs_fixture_itself(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Child command run by hand has no Parent payload: plain pytest."""
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(_TWO_GROUPS)
        monkeypatch.setenv("PYTEST_RUNNING_IN_SUBPROCESS", "1")

        result = pytester.runpytest_subprocess()

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester).count("child:setup") == 1

    def test_override_in_subdirectory_is_not_shared(self, pytester: Pytester) -> None:
        """Sharing belongs to a fixture definition, not to its name."""
        pytester.makeconftest(_SHARED_CONFTEST)
        sub = pytester.mkdir("sub")
        (sub / "conftest.py").write_text(
            """
import pytest


@pytest.fixture(scope="session")
def server():
    return {"url": "local"}
"""
        )
        (sub / "test_sub.py").write_text(
            """
import pytest


@pytest.mark.isolated
def test_sub(server):
    assert server == {"url": "local"}
"""
        )
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest()

        result.assert_outcomes(passed=3)
        assert _log_lines(pytester).count("parent:setup") == 1
        assert "child:setup" not in _log_lines(pytester)

    def test_pre_skipped_items_do_not_set_up_shared(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
@pytest.mark.skip(reason="never runs")
def test_skipped(server):
    pass


@pytest.mark.isolated
def test_live():
    pass
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=1, skipped=1)
        assert "parent:setup" not in _log_lines(pytester)

    def test_maxfail_still_tears_down_before_sessionfinish(
        self, pytester: Pytester
    ) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(
            test_a="""
import pytest


@pytest.mark.isolated(group="a")
def test_fails(server):
    raise RuntimeError("boom")
""",
            test_b="""
import pytest


@pytest.mark.isolated(group="b")
def test_never_launched(server):
    pass
""",
        )

        result = pytester.runpytest("--maxfail=1")

        assert result.ret == pytest.ExitCode.TESTS_FAILED
        result.assert_outcomes(failed=1)
        lines = _log_lines(pytester)
        assert lines.index("parent:teardown") < lines.index("parent:sessionfinish")

    @pytest.mark.parametrize("source", ["cli", "ini"])
    def test_serial_children_preserve_parent_temporary_resources(
        self, pytester: Pytester, source: str
    ) -> None:
        """A Child's basetemp cleanup must not delete a live Parent resource."""
        base = pytester.path / "shared-base"
        pytester.makeconftest(
            """
from pathlib import Path

import pytest
import pytest_isolated


@pytest.fixture(scope="session")
@pytest_isolated.shared
def resource(tmp_path_factory):
    path = tmp_path_factory.getbasetemp() / "parent-resource.txt"
    path.write_text("live")
    yield str(path)
    assert path.read_text() == "live"
    Path("parent.finalized").write_text("done")
"""
        )
        pytester.makepyfile(
            """
from pathlib import Path

import pytest


@pytest.mark.isolated
def test_first_child(resource, tmp_path):
    assert Path(resource).read_text() == "live"


@pytest.mark.isolated
def test_second_child(resource, tmp_path):
    assert Path(resource).read_text() == "live"
"""
        )

        if source == "cli":
            result = pytester.runpytest("--basetemp", str(base))
        else:
            pytester.makeini(f'[pytest]\naddopts = --basetemp="{base}"\n')
            # pytester's runpytest helpers inject their own CLI basetemp.
            result = pytester.run(sys.executable, "-m", "pytest")

        result.assert_outcomes(passed=2)
        assert (pytester.path / "parent.finalized").read_text() == "done"


@REQUIRES_PARENT_FIXTURES
class TestSharedFixtureOutcomes:
    """Parent outcomes reach exactly the Items that use the fixture."""

    def test_setup_failure_errors_only_dependent_items(
        self, pytester: Pytester
    ) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session")
@pytest_isolated.shared
def server():
    _log("setup")
    raise RuntimeError("server did not start")
"""
        )
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated(group="a")
def test_uses(server):
    pass


@pytest.mark.isolated(group="a")
def test_uses_too(server):
    pass


@pytest.mark.isolated(group="b")
def test_other():
    pass
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=1, errors=2)
        output = _output(result)
        assert "failed in the Parent process" in output
        assert "server did not start" in output
        assert _log_lines(pytester).count("parent:setup") == 1
        assert "child:setup" not in _log_lines(pytester)

    def test_setup_skip_skips_dependent_items(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            """
import pytest
import pytest_isolated


@pytest.fixture(scope="session")
@pytest_isolated.shared
def server():
    pytest.skip("no docker")
"""
        )
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
def test_uses(server):
    pass


@pytest.mark.isolated
def test_other():
    pass
"""
        )

        result = pytester.runpytest("-rs")

        result.assert_outcomes(passed=1, skipped=1)
        assert "no docker" in _output(result)

    def test_teardown_failure_reported_once(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            """
import pytest
import pytest_isolated


@pytest.fixture(scope="session")
@pytest_isolated.shared
def server():
    yield "up"
    pytest.fail("teardown boom")
"""
        )
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated(group="a")
def test_a(server):
    assert server == "up"


@pytest.mark.isolated(group="b")
def test_b(server):
    assert server == "up"
"""
        )

        result = pytester.runpytest()

        result.assert_outcomes(passed=2, errors=1)
        assert "teardown boom" in _output(result)
        assert "INTERNALERROR" not in _output(result)


@REQUIRES_PARENT_FIXTURES
class TestSharedFixtureUsageErrors:
    """Fixtures that cannot have one shared value fail before any Child."""

    @pytest.mark.parametrize(
        ("decorator", "expected"),
        [
            ('@pytest.fixture(scope="module")', "must be scope='session'"),
            ('@pytest.fixture(scope="session", params=[1, 2])', "parametrized"),
        ],
    )
    def test_invalid_definition(
        self, pytester: Pytester, decorator: str, expected: str
    ) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + f"""

{decorator}
@pytest_isolated.shared
def server():
    _log("setup")
"""
        )
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest()

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        assert expected in _output(result)
        assert "parent:setup" not in _log_lines(pytester)
        assert not [line for line in _log_lines(pytester) if "child" in line]

    def test_indirect_parametrization(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(
            """
import pytest


@pytest.mark.isolated
@pytest.mark.parametrize("server", [1], indirect=True)
def test_one(server):
    pass
"""
        )

        result = pytester.runpytest()

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        assert "parametrized" in _output(result)

    def test_non_json_value(self, pytester: Pytester) -> None:
        pytester.makeconftest(
            _LOG_HELPER
            + """

@pytest.fixture(scope="session")
@pytest_isolated.shared
def server():
    yield object()
    _log("teardown")
"""
        )
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest()

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        assert "JSON-serializable" in _output(result)
        # Partial setup is still torn down (on pytest's error path, after
        # sessionfinish hooks as in stock pytest), and no Child launched.
        assert sorted(_log_lines(pytester)) == [
            "parent:sessionfinish",
            "parent:teardown",
        ]


class TestSharedDecorator:
    def test_returns_fixture_unchanged(self) -> None:
        def raw() -> int:
            return 1

        assert shared(raw) is raw
        assert raw._pytest_isolated_shared is True  # type: ignore[attr-defined]


class TestPytestVersionGate:
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
    def test_pytest7_falls_back_to_per_child(self, pytester: Pytester) -> None:
        pytester.makeconftest(_SHARED_CONFTEST)
        pytester.makepyfile(_TWO_GROUPS)

        result = pytester.runpytest_subprocess()

        result.assert_outcomes(passed=2)
        assert _log_lines(pytester).count("child:setup") == 2
        assert "require pytest>=8" in _output(result)


class TestPytestInternalsPresence:
    """Pin the private pytest surface ``shared_fixtures`` relies on, so drift
    fails this suite on a pytest upgrade instead of in the field."""

    def test_skipping_helpers(self) -> None:
        assert hasattr(skipping, "evaluate_skip_marks")
        assert hasattr(skipping, "evaluate_xfail_marks")

    def test_fixture_info_pruning(self) -> None:
        assert hasattr(FuncFixtureInfo, "prune_dependency_tree")
        assert "name2fixturedefs" in inspect.signature(FuncFixtureInfo).parameters

    @REQUIRES_PARENT_FIXTURES
    def test_fixture_request_constructor(self) -> None:
        parameters = inspect.signature(FixtureRequest.__init__).parameters
        assert {
            "pyfuncitem",
            "fixturename",
            "arg2fixturedefs",
            "fixture_defs",
            "_ispytest",
        } <= set(parameters)

    def test_setup_state_stack(self) -> None:
        assert "stack" in inspect.getsource(SetupState.__init__)


def test_payload_path_reaches_child_env(pytester: Pytester, tmp_path: Path) -> None:
    config = pytester.parseconfig()
    payload = tmp_path / "payload.json"

    assert plan_launch(config, ["x"], shared_payload=payload).env[
        "PYTEST_ISOLATED_SHARED_FIXTURES"
    ] == str(payload)
    assert "PYTEST_ISOLATED_SHARED_FIXTURES" not in plan_launch(config, ["x"]).env
