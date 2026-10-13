"""Unit/e2e tests for the child_args interface.

Spec: issue #64

Seam A (primary, e2e): what the child *received*, observed through a fixture
inside an isolated test.
Seam B (unit): child_args(real_config) -> list[str], the interface itself.
Never token-scanning internals.
"""

from __future__ import annotations

import re
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


class TestEndOfOptionsMarker:
    """'--' stops option scanning: everything after it is a parent-side selector.

    Forwarding the marker (or tokens after it) would make the child treat the
    appended rootdir/nodeids as positional arguments and fail collection.
    """

    def test_double_dash_and_following_tokens_never_forwarded(
        self, pytester: Pytester
    ) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("-v", "--", "test_iso.py")

        assert _meaningful(child_args(config)) == ["-v"]

    def test_option_like_tokens_after_double_dash_are_selectors(
        self, pytester: Pytester
    ) -> None:
        """After '--' even '-k slow' is a selector to pytest, not an option."""
        _isolated_file(pytester)
        config = pytester.parseconfig("--", "-k", "slow", "test_iso.py")

        assert _meaningful(child_args(config)) == []

    def test_double_dash_invocation_collects_and_passes_end_to_end(
        self, pytester: Pytester
    ) -> None:
        """Seam A: 'pytest -- test_iso.py' still isolates and runs the child."""
        _isolated_file(pytester)

        result = pytester.runpytest("--", "test_iso.py")

        result.assert_outcomes(passed=1)


class TestShortOptionClusters:
    """Cluster arity mirrors argparse: only a value-taking *final* option
    consumes a following token; a value option earlier in the cluster absorbs
    the cluster's own tail inline and stays self-contained."""

    def test_cluster_with_trailing_value_option_keeps_its_value(
        self, pytester: Pytester
    ) -> None:
        """argparse reads '-qk expr' as -q plus -k expr: expr must not be
        discarded as a positional selector."""
        _isolated_file(pytester)
        config = pytester.parseconfig("-qk", "expr", "test_iso.py")

        assert _meaningful(child_args(config)) == ["-qk", "expr"]

    def test_all_flag_cluster_forwards_alone(self, pytester: Pytester) -> None:
        _isolated_file(pytester)
        config = pytester.parseconfig("-xv", "test_iso.py")

        assert _meaningful(child_args(config)) == ["-xv"]

    def test_value_option_inside_cluster_absorbs_tail_inline(
        self, pytester: Pytester
    ) -> None:
        """argparse reads '-kk expr' as -k='k' (self-contained); 'expr' was a
        selector to the parent, so the child must not receive it."""
        _isolated_file(pytester)
        config = pytester.parseconfig("-kk", "expr", "test_iso.py")

        assert _meaningful(child_args(config)) == ["-kk"]

    def test_child_receives_clustered_option_value_end_to_end(
        self, pytester: Pytester
    ) -> None:
        """Seam A: the isolated test reads -k through a fixture, proving the
        child parsed the cluster the same way the parent did."""
        pytester.makepyfile(
            """
            import pytest


            @pytest.fixture
            def keyword(request):
                return request.config.getoption("-k")


            @pytest.mark.isolated
            def test_expr_child_sees_cluster(keyword):
                assert keyword == "expr"
            """
        )

        result = pytester.runpytest("-qk", "expr")

        result.assert_outcomes(passed=1)


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
    """Each documented category holds exactly its policy-table options.

    Set equality in both directions: a table option missing from its category
    section, a section entry with no table row (stale or reclassified), and an
    option documented under the wrong category all fail here.
    """

    @classmethod
    def _section(cls, header: str) -> str:
        lines = (
            (Path(__file__).resolve().parents[1] / "docs" / "incompatible-options.md")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        start = next(
            i for i, line in enumerate(lines) if line.startswith(f"## {header}")
        )
        end = next(
            (
                i
                for i, line in enumerate(lines[start + 1 :], start + 1)
                if line.startswith("## ")
            ),
            len(lines),
        )
        return "\n".join(lines[start:end])

    @staticmethod
    def _documented_options(section: str) -> set[str]:
        # Backticked option strings; '=...' value suffixes normalized away
        # (e.g. '--capture=...' documents the '--capture' row).
        return {found.partition("=")[0] for found in re.findall(r"`(-[^`]*)`", section)}

    def test_category_a_is_exactly_the_blocked_options(self) -> None:
        documented = self._documented_options(self._section("Category A"))
        assert documented == set(BLOCKED_OPTIONS)

    def test_category_b_is_exactly_the_parent_handled_options(self) -> None:
        documented = self._documented_options(self._section("Category B"))
        assert documented == set(PARENT_HANDLED_OPTIONS)
