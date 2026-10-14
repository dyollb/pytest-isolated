"""Unit tests for the plan_launch interface.

Spec: issue #65.

Seam: plan_launch(real_config, nodeid_strings) -> LaunchSpec, asserted
field-by-field (cmd, env, cwd, report_path). No mocks, no spawned children —
the child's whole interface is a plain value. The pytester e2e suite
(tests/test_execution.py, tests/test_options.py) remains the
behavior-preservation proof.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from pytest import Pytester

from pytest_isolated.config import SUBPROC_ENV, SUBPROC_REPORT_PATH
from pytest_isolated.launch import LaunchSpec, plan_launch

# parseconfig always injects these; not interesting for the assertions below.
_NOISE_PREFIXES = ("--basetemp",)

# The launcher's fixed base command, asserted as a literal; cmd order is
# base flags, capture policy, forwarded args, nodeids.
_BASE_CMD = [sys.executable, "-u", "-m", "pytest"]


def _meaningful(args: list[str]) -> list[str]:
    """Drop parseconfig noise, as in tests/test_child_args.py."""
    out: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith(_NOISE_PREFIXES):
            continue
        if arg == "--rootdir":
            skip_next = True  # its value; asserted separately
            continue
        out.append(arg)
    return out


def _isolated_file(pytester: Pytester) -> None:
    pytester.makepyfile(
        test_iso=textwrap.dedent(
            """
            import pytest

            @pytest.mark.isolated
            def test_one():
                assert True
            """
        )
    )


class TestCommandAssembly:
    """cmd is base flags, capture policy, forwarded args, nodeids — in order."""

    def test_base_command_prefix(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert isinstance(spec, LaunchSpec)
        assert spec.cmd[: len(_BASE_CMD)] == _BASE_CMD

    def test_capture_tee_sys_by_default(self, pytester: Pytester) -> None:
        """The partial-output invariant: -u (in the base) + tee-sys together."""
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.cmd[len(_BASE_CMD)] == "--capture=tee-sys"

    @pytest.mark.parametrize("no_capture", ["-s", "--capture=no"])
    def test_user_no_capture_is_respected(
        self, pytester: Pytester, no_capture: str
    ) -> None:
        """The #28 carve-out: -s/--capture=no on the parent -> -s on the child."""
        _isolated_file(pytester)
        config = pytester.parseconfig(no_capture, "test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.cmd[len(_BASE_CMD)] == "-s"

    def test_nodeids_appended_last_in_order(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")
        nodeids = ["test_iso.py::test_one", "other.py::test_two"]

        spec = plan_launch(config, nodeids)

        assert spec.cmd[-2:] == nodeids

    def test_forwarded_args_come_before_nodeids(self, pytester: Pytester) -> None:
        """Argv order is a launcher invariant: base, capture, forwarded, nodeids."""
        _isolated_file(pytester)
        config = pytester.parseconfig("-v", "test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        rest = _meaningful(spec.cmd[len(_BASE_CMD) :])  # after base command
        assert rest == ["--capture=tee-sys", "-v", "test_iso.py::test_one"]

    def test_rootdir_stated_exactly_once_in_cmd(self, pytester: Pytester) -> None:
        """--rootdir arrives from child_args once; the launcher adds none."""
        _isolated_file(pytester)
        config = pytester.parseconfig("--rootdir=.", "test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.cmd.count("--rootdir") == 1
        assert spec.cmd[spec.cmd.index("--rootdir") + 1] == str(config.rootpath)


class TestEnvContract:
    """The child env is the parent's plus the two plugin subprocess variables."""

    def test_subprocess_marker_and_report_path_set(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.env[SUBPROC_ENV] == "1"
        assert spec.env[SUBPROC_REPORT_PATH] == spec.report_path

    def test_parent_environment_inherited(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever the parent has (incl. PYTEST_ADDOPTS) reaches the child."""
        monkeypatch.setenv("Z43_SENTINEL", "inherited")
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.env["Z43_SENTINEL"] == "inherited"


class TestWorkingDirectory:
    """cwd is the parent's resolved rootpath (invocation dir as fallback)."""

    def test_cwd_is_rootpath(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.cwd == str(config.rootpath)


class TestReportChannel:
    """Fresh JSONL report file per plan, passed via the env."""

    def test_report_path_is_fresh_jsonl_file(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert spec.report_path.endswith(".jsonl")
        assert Path(spec.report_path).exists()

    def test_two_calls_produce_distinct_report_paths(self, pytester: Pytester) -> None:
        """One group's results must never leak into another's classification."""
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        first = plan_launch(config, ["test_iso.py::test_one"])
        second = plan_launch(config, ["test_iso.py::test_one"])

        assert first.report_path != second.report_path
