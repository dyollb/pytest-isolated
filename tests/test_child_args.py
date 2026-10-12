"""Unit/e2e tests for the child_args interface.

Spec: docs/spec-child-args-forwarding.md

Seam A (primary, e2e): what the child *received*, observed through a fixture
inside an isolated test.
Seam B (unit): child_args(real_config) -> list[str], the interface itself.
Never token-scanning internals.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pytest import Pytester

from pytest_isolated.forwarding import (
    BLOCKED_OPTIONS,
    PARENT_HANDLED_OPTIONS,
    child_args,
    validate_isolation_compatibility,
)

# parseconfig always injects these; not interesting for the assertions below.
_NOISE_PREFIXES = ("--basetemp",)


def _meaningful(args: list[str]) -> list[str]:
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


def _register(pytester: Pytester, *names: str) -> None:
    """Register store-style options via conftest (real parser arity)."""
    lines = "\n".join(
        f'    parser.addoption("{name}", action="store", default=None)'
        for name in names
    )
    pytester.makeconftest(
        textwrap.dedent(
            f"""
            import pytest


            def pytest_addoption(parser):
            {lines}
            """
        )
    )


class TestThirdPartyOptionsKeepTheirValues:
    """The dropped-value bug class: arity comes from the parser."""

    def test_separate_token_value_is_forwarded(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        _register(pytester, "--slowest")
        config = pytester.parseconfig("--slowest", "5", "test_iso.py")

        assert _meaningful(child_args(config)) == ["--slowest", "5"]

    def test_equals_form_is_forwarded(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        _register(pytester, "--randomly-seed")
        config = pytester.parseconfig("--randomly-seed=42", "test_iso.py")

        assert _meaningful(child_args(config)) == ["--randomly-seed=42"]

    def test_repeatable_value_option_repeated(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig(
            "-o",
            "console_output_style=classic",
            "-o",
            "cache_dir=.mycache",
            "test_iso.py",
        )

        assert _meaningful(child_args(config)) == [
            "-o",
            "console_output_style=classic",
            "-o",
            "cache_dir=.mycache",
        ]

    def test_custom_store_option_equals_form(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        _register(pytester, "--eval-solution-path")
        config = pytester.parseconfig("--eval-solution-path=/tmp/s", "test_iso.py")

        assert _meaningful(child_args(config)) == ["--eval-solution-path=/tmp/s"]

    def test_child_receives_option_value_end_to_end(self, pytester: Pytester) -> None:
        """Seam A: the isolated test reads the option through a fixture, so the
        test only passes if the child actually got '--slowest 5'."""
        _register(pytester, "--slowest")
        pytester.makepyfile(
            """
            import pytest


            @pytest.fixture
            def slowest(request):
                return request.config.getoption("--slowest")


            @pytest.mark.isolated
            def test_child_sees_value(slowest):
                assert slowest == "5"
            """
        )

        result = pytester.runpytest("-v", "--slowest", "5")
        result.assert_outcomes(passed=1)


class TestPolicyClassification:
    """Blocked / parent-handled / forwarded all come from one table."""

    def test_parent_handled_flags_not_forwarded(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig(
            "-s", "--ignore", "docs", "--isolated", "-v", "test_iso.py"
        )

        assert _meaningful(child_args(config)) == ["-v"]

    def test_parent_handled_equals_form_not_forwarded(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("--ignore=docs", "--capture=no", "test_iso.py")

        assert _meaningful(child_args(config)) == []

    def test_positional_selectors_never_forwarded(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig(
            "-v", "test_iso.py", "test_iso.py::test_one", "other.py"
        )

        assert _meaningful(child_args(config)) == ["-v"]

    def test_blocked_option_dropped_defensively(self, pytester: Pytester) -> None:
        """Validation normally rejects blocked options before this point; the
        interface still never emits them (incl. their values)."""
        _isolated_file(pytester)
        config = pytester.parseconfig("--pdbcls", "mod:Cls", "-v", "test_iso.py")

        assert _meaningful(child_args(config)) == ["-v"]


class TestRootdirStatedOnce:
    """The parent's resolved rootpath is the single rootdir source."""

    def test_parent_rootpath_appended_exactly_once(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("-v", "test_iso.py")
        args = child_args(config)

        assert args.count("--rootdir") == 1
        assert args[args.index("--rootdir") + 1] == str(config.rootpath)

    def test_user_rootdir_replaced_by_parent_rootpath(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("--rootdir=.", "test_iso.py")
        args = child_args(config)

        assert "--rootdir=." not in args
        assert args.count("--rootdir") == 1
        assert args[args.index("--rootdir") + 1] == str(config.rootpath)


class TestValidationReadsTheTable:
    """One enforcement point: CLI args and PYTEST_ADDOPTS, parsed state too."""

    def test_blocked_option_on_cli_raises(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        config = pytester.parseconfig("-v", "--runxfail")

        with pytest.raises(pytest.UsageError) as excinfo:
            validate_isolation_compatibility(config)
        assert "--runxfail" in str(excinfo.value)
        assert "--no-isolation" in str(excinfo.value)

    def test_aliased_blocked_option_named_as_typed(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--nf and --new-first share one parsed destination; the error names
        the option the user actually wrote, not whichever table row is first."""
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        config = pytester.parseconfig("--new-first")

        with pytest.raises(pytest.UsageError) as excinfo:
            validate_isolation_compatibility(config)
        message = str(excinfo.value)
        assert "Option '--new-first'" in message
        assert "'--nf'" not in message

    def test_blocked_option_in_addopts_raises(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--setup-only")
        config = pytester.parseconfig("-v")

        with pytest.raises(pytest.UsageError) as excinfo:
            validate_isolation_compatibility(config)
        assert "--setup-only" in str(excinfo.value)

    def test_parsed_pdb_state_raises_with_isolation_hint(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--pdb is enforced via the table row's parsed-option check, so it is
        caught wherever it came from (single gate replacing the old split)."""
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
        config = pytester.parseconfig("--pdb", "-v")

        with pytest.raises(pytest.UsageError) as excinfo:
            validate_isolation_compatibility(config)
        assert "--pdb" in str(excinfo.value)
        assert "--no-isolation" in str(excinfo.value)

    def test_pdb_via_addopts_raises(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--pdb")
        config = pytester.parseconfig("-v")

        with pytest.raises(pytest.UsageError) as excinfo:
            validate_isolation_compatibility(config)
        assert "--pdb" in str(excinfo.value)

    def test_forwarded_options_pass_validation(
        self, pytester: Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYTEST_ADDOPTS", "--tb=long")
        config = pytester.parseconfig("-v", "--continue-on-collection-errors")

        validate_isolation_compatibility(config)  # must not raise


class TestDocsMatchTable:
    """The compatibility doc cannot drift from the policy table."""

    @staticmethod
    def _doc() -> list[str]:
        return (
            (Path(__file__).resolve().parents[1] / "docs" / "incompatible-options.md")
            .read_text(encoding="utf-8")
            .splitlines()
        )

    def test_every_blocked_option_documented(self) -> None:
        text = "\n".join(self._doc())
        missing = [name for name in BLOCKED_OPTIONS if f"`{name}`" not in text]
        assert not missing, f"blocked options missing from docs: {missing}"

    def test_every_parent_handled_option_documented(self) -> None:
        lines = self._doc()
        start = next(
            i for i, line in enumerate(lines) if line.startswith("## Category B")
        )
        end = next(
            i
            for i, line in enumerate(lines[start + 1 :], start + 1)
            if line.startswith("## ")
        )
        category_b = "\n".join(lines[start:end])
        missing = [
            name for name in PARENT_HANDLED_OPTIONS if f"`{name}`" not in category_b
        ]
        assert not missing, (
            f"parent-handled options missing from Category B docs: {missing}"
        )
