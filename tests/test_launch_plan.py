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
from pytest_isolated.launch import Hardened, LaunchSpec, plan_launch

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


class TestParallelHardening:
    """The hardened plan form: root + slot, named inside the module.

    Slot-to-directory naming is private to the Launch plan module; its only
    interface is the argv, asserted here. Spec:
    https://github.com/dyollb/pytest-isolated/issues/73
    """

    def test_default_plan_has_no_injection(self, pytester: Pytester) -> None:
        """The serial regression contract: plain call -> pre-parallel argv."""
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")

        spec = plan_launch(config, ["test_iso.py::test_one"])

        assert "--basetemp" not in spec.cmd
        assert "no:cacheprovider" not in spec.cmd
        assert not any("cache_dir" in tok for tok in spec.cmd)
        assert not any("lfnf" in tok for tok in spec.cmd)

    def test_serial_basetemp_override_preserves_cache_policy(
        self, pytester: Pytester, tmp_path: Path
    ) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig(
            "--basetemp=original",
            "-o",
            "cache_dir=shared-cache",
            "--lfnf=none",
            "test_iso.py",
        )

        spec = plan_launch(
            config,
            ["test_iso.py::test_one"],
            hardened=Hardened(root=tmp_path, slot=2, isolate_cache=False),
        )

        assert spec.cmd[-3:] == [
            "--basetemp",
            str(tmp_path / "group-2"),
            "test_iso.py::test_one",
        ]
        assert "cache_dir=shared-cache" in spec.cmd
        assert not any("cache-2" in token for token in spec.cmd)
        assert "--lfnf=none" in spec.cmd
        assert "--lfnf" not in spec.cmd

    def test_lfnf_none_policy_gets_fresh_cache_override(
        self, pytester: Pytester, tmp_path: Path
    ) -> None:
        """With the Parent under --lfnf=none, the Child's fresh cache needs
        the 'all' empty-cache policy or --lf deselects the Parent's own
        selection; the override rides with the other hardened tokens."""
        _isolated_file(pytester)
        config = pytester.parseconfig("--lfnf=none", "test_iso.py")
        root = tmp_path / "grp"

        spec = plan_launch(
            config,
            ["test_iso.py::test_one"],
            hardened=Hardened(root=root, slot=0),
        )

        assert spec.cmd[-7:-1] == [
            "-o",
            f"cache_dir={root / 'cache-0'}",
            "--lfnf",
            "all",
            "--basetemp",
            str(root / "group-0"),
        ]

    def test_hardened_plan_carries_slot_derived_paths_after_forwarded_args(
        self, pytester: Pytester, tmp_path: Path
    ) -> None:
        """Last-wins: hardened tokens must beat any forwarded --basetemp/-o,
        so they append after the forwarded args and before the nodeids."""
        _isolated_file(pytester)
        config = pytester.parseconfig("-v", "test_iso.py")
        root = tmp_path / "grp"

        spec = plan_launch(
            config,
            ["test_iso.py::test_one"],
            hardened=Hardened(root=root, slot=3),
        )

        cmd = spec.cmd
        assert cmd[-1] == "test_iso.py::test_one"
        # Path-native expected values: the naming (group-N / cache-N) is the
        # literal from the spec; the separator style is the platform's own.
        assert cmd[-5:-1] == [
            "-o",
            f"cache_dir={root / 'cache-3'}",
            "--basetemp",
            str(root / "group-3"),
        ]

    def test_two_slots_derive_distinct_paths(
        self, pytester: Pytester, tmp_path: Path
    ) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("test_iso.py")
        root = tmp_path / "grp"

        first = plan_launch(
            config,
            ["test_iso.py::test_one"],
            hardened=Hardened(root=root, slot=0),
        )
        second = plan_launch(
            config,
            ["test_iso.py::test_one"],
            hardened=Hardened(root=root, slot=1),
        )

        assert first.cmd[-3:-1] == ["--basetemp", str(root / "group-0")]
        assert second.cmd[-3:-1] == ["--basetemp", str(root / "group-1")]
        assert f"cache_dir={root / 'cache-0'}" in first.cmd
        assert f"cache_dir={root / 'cache-1'}" in second.cmd
