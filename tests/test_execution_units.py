"""Unit tests for execution module helper functions."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from pytest_isolated.execution import (
    _build_forwarded_args,
    _detect_crashed_tests,
    _parse_results,
)


class TestBuildForwardedArgs:
    """Test _build_forwarded_args function."""

    @pytest.mark.parametrize(
        ("flags", "expected_forwarded"),
        [
            (["-v", "tests/"], ["-v"]),
            (["-s", "-x", "tests/"], ["-s", "-x"]),
            (["-v", "-v", "-v"], ["-v", "-v", "-v"]),
            (["-q", "-l"], ["-q", "-l"]),
            (["--verbose", "--exitfirst"], ["--verbose", "--exitfirst"]),
        ],
    )
    def test_forwards_flag_options(
        self, flags: list[str], expected_forwarded: list[str]
    ) -> None:
        """Test that flags are forwarded correctly."""
        config = Mock()
        config.invocation_params.args = flags

        result = _build_forwarded_args(config)

        assert result == expected_forwarded

    @pytest.mark.parametrize(
        ("args", "expected_option", "expected_value"),
        [
            (["--tb", "short", "tests/"], "--tb", "short"),
            (["--capture", "sys", "tests/"], "--capture", "sys"),
            (["-r", "fE"], "-r", "fE"),
        ],
    )
    def test_forwards_options_with_values_separate(
        self, args: list[str], expected_option: str, expected_value: str
    ) -> None:
        """Test that options with values are forwarded."""
        config = Mock()
        config.invocation_params.args = args

        result = _build_forwarded_args(config)

        assert expected_option in result
        assert expected_value in result
        opt_idx = result.index(expected_option)
        assert result[opt_idx + 1] == expected_value

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["--capture=sys", "tests/"], ["--capture=sys"]),
            (["--tb=short", "tests/"], ["--tb=short"]),
            (["--capture=no", "-r=fE"], ["--capture=no", "-r=fE"]),
        ],
    )
    def test_forwards_options_with_values_combined(
        self, args: list[str], expected: list[str]
    ) -> None:
        """Test that combined options like --capture=sys are forwarded."""
        config = Mock()
        config.invocation_params.args = args

        result = _build_forwarded_args(config)

        for exp in expected:
            assert exp in result

    def test_excludes_test_paths(self) -> None:
        """Test that positional test paths are not forwarded."""
        config = Mock()
        config.invocation_params.args = [
            "-v",
            "tests/test_foo.py",
            "tests/test_bar.py::test_baz",
        ]

        result = _build_forwarded_args(config)

        assert "-v" in result
        assert "tests/test_foo.py" not in result
        assert "tests/test_bar.py::test_baz" not in result

    def test_excludes_unknown_options(self) -> None:
        """Test that unknown options are not forwarded."""
        config = Mock()
        config.invocation_params.args = [
            "-v",
            "--unknown-option",
            "--isolated",
            "value",
        ]

        result = _build_forwarded_args(config)

        assert "-v" in result
        assert "--unknown-option" not in result
        assert "--isolated" not in result

    def test_handles_empty_args(self) -> None:
        """Test handling of empty args list."""
        config = Mock()
        config.invocation_params.args = []

        result = _build_forwarded_args(config)

        assert result == []


class TestParseResults:
    """Test _parse_results function."""

    def test_parses_valid_jsonl_single_test(self, tmp_path: Path) -> None:
        """Test parsing valid JSONL with single test."""
        report_file = tmp_path / "report.jsonl"
        report_file.write_text(
            json.dumps(
                {
                    "nodeid": "test_foo.py::test_one",
                    "when": "setup",
                    "outcome": "passed",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "nodeid": "test_foo.py::test_one",
                    "when": "call",
                    "outcome": "passed",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "nodeid": "test_foo.py::test_one",
                    "when": "teardown",
                    "outcome": "passed",
                }
            )
            + "\n"
        )

        result = _parse_results(str(report_file))

        assert "test_foo.py::test_one" in result
        assert "setup" in result["test_foo.py::test_one"]
        assert "call" in result["test_foo.py::test_one"]
        assert "teardown" in result["test_foo.py::test_one"]
        assert result["test_foo.py::test_one"]["setup"]["outcome"] == "passed"
        assert result["test_foo.py::test_one"]["call"]["outcome"] == "passed"

    def test_parses_multiple_tests(self, tmp_path: Path) -> None:
        """Test parsing JSONL with multiple tests."""
        report_file = tmp_path / "report.jsonl"
        report_file.write_text(
            json.dumps(
                {"nodeid": "test_foo.py::test_one", "when": "call", "outcome": "passed"}
            )
            + "\n"
            + json.dumps(
                {"nodeid": "test_foo.py::test_two", "when": "call", "outcome": "failed"}
            )
            + "\n"
        )

        result = _parse_results(str(report_file))

        assert "test_foo.py::test_one" in result
        assert "test_foo.py::test_two" in result
        assert result["test_foo.py::test_one"]["call"]["outcome"] == "passed"
        assert result["test_foo.py::test_two"]["call"]["outcome"] == "failed"

    def test_handles_empty_file(self, tmp_path: Path) -> None:
        """Test parsing empty JSONL file."""
        report_file = tmp_path / "report.jsonl"
        report_file.write_text("")

        result = _parse_results(str(report_file))

        assert result == {}

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        """Test parsing non-existent file."""
        report_file = tmp_path / "nonexistent.jsonl"

        result = _parse_results(str(report_file))

        assert result == {}

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        """Test that empty lines are skipped."""
        report_file = tmp_path / "report.jsonl"
        report_file.write_text(
            json.dumps(
                {"nodeid": "test_foo.py::test_one", "when": "call", "outcome": "passed"}
            )
            + "\n"
            + "\n"
            + "   \n"
            + json.dumps(
                {"nodeid": "test_foo.py::test_two", "when": "call", "outcome": "passed"}
            )
            + "\n"
        )

        result = _parse_results(str(report_file))

        assert len(result) == 2
        assert "test_foo.py::test_one" in result
        assert "test_foo.py::test_two" in result

    def test_parses_all_phases(self, tmp_path: Path) -> None:
        """Test parsing all test phases (setup, call, teardown)."""
        report_file = tmp_path / "report.jsonl"
        data = [
            {
                "nodeid": "test.py::test_x",
                "when": "setup",
                "outcome": "passed",
                "duration": 0.1,
            },
            {
                "nodeid": "test.py::test_x",
                "when": "call",
                "outcome": "failed",
                "longrepr": "assert False",
            },
            {
                "nodeid": "test.py::test_x",
                "when": "teardown",
                "outcome": "passed",
                "duration": 0.05,
            },
        ]
        report_file.write_text("\n".join(json.dumps(d) for d in data) + "\n")

        result = _parse_results(str(report_file))

        assert "test.py::test_x" in result
        assert len(result["test.py::test_x"]) == 3
        assert result["test.py::test_x"]["setup"]["duration"] == 0.1
        assert result["test.py::test_x"]["call"]["longrepr"] == "assert False"
        assert result["test.py::test_x"]["teardown"]["outcome"] == "passed"

    def test_deletes_file_after_parsing(self, tmp_path: Path) -> None:
        """Test that the report file is deleted after parsing."""
        report_file = tmp_path / "report.jsonl"
        report_file.write_text(
            json.dumps(
                {"nodeid": "test.py::test_x", "when": "call", "outcome": "passed"}
            )
            + "\n"
        )

        _parse_results(str(report_file))

        assert not report_file.exists()


class TestDetectCrashedTests:
    """Test _detect_crashed_tests function."""

    def _make_item(self, nodeid: str, xfail: bool = False) -> Mock:
        """Create a mock pytest.Item."""
        item = Mock()
        item.nodeid = nodeid
        if xfail:
            item.get_closest_marker.return_value = Mock()  # xfail marker
        else:
            item.get_closest_marker.return_value = None
        return item

    def test_detects_crash_when_setup_passed_no_call(self) -> None:
        """Test detecting crash when setup passed but call phase missing."""
        items = [self._make_item("test.py::test_crash")]
        results = {
            "test.py::test_crash": {
                "setup": {"outcome": "passed"},
                # Missing "call" phase indicates crash
            }
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 1
        assert crashed[0].nodeid == "test.py::test_crash"
        assert len(not_run) == 0

    @pytest.mark.parametrize(
        "setup_outcome",
        ["failed", "skipped", "error"],
    )
    def test_no_crash_when_setup_not_passed(self, setup_outcome: str) -> None:
        """Test that non-passed setup + missing call is not a crash."""
        items = [self._make_item("test.py::test_no_crash")]
        results = {
            "test.py::test_no_crash": {
                "setup": {"outcome": setup_outcome},
                # Missing "call" is expected when setup doesn't pass
            }
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 0
        assert len(not_run) == 0

    def test_detects_not_run_tests_after_crash(self) -> None:
        """Test detecting tests that never started after a crash."""
        items = [
            self._make_item("test.py::test_one"),
            self._make_item("test.py::test_crash"),
            self._make_item("test.py::test_never_run"),
        ]
        results = {
            "test.py::test_one": {
                "setup": {"outcome": "passed"},
                "call": {"outcome": "passed"},
                "teardown": {"outcome": "passed"},
            },
            "test.py::test_crash": {
                "setup": {"outcome": "passed"},
                # Missing "call" - crashed here
            },
            # "test.py::test_never_run" has no results at all
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 1
        assert crashed[0].nodeid == "test.py::test_crash"
        assert len(not_run) == 1
        assert not_run[0].nodeid == "test.py::test_never_run"

    def test_no_crash_when_all_phases_present(self) -> None:
        """Test that complete test results are not flagged as crashed."""
        items = [self._make_item("test.py::test_complete")]
        results = {
            "test.py::test_complete": {
                "setup": {"outcome": "passed"},
                "call": {"outcome": "failed"},
                "teardown": {"outcome": "passed"},
            }
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 0
        assert len(not_run) == 0

    def test_multiple_crashed_tests(self) -> None:
        """Test detecting multiple crashed tests in a group."""
        items = [
            self._make_item("test.py::test_crash_one"),
            self._make_item("test.py::test_crash_two"),
        ]
        results = {
            "test.py::test_crash_one": {"setup": {"outcome": "passed"}},
            "test.py::test_crash_two": {"setup": {"outcome": "passed"}},
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 2
        assert len(not_run) == 0

    def test_empty_results(self) -> None:
        """Test with no results at all (e.g., collection crash)."""
        items = [
            self._make_item("test.py::test_one"),
            self._make_item("test.py::test_two"),
        ]
        results: dict[str, Any] = {}

        crashed, not_run = _detect_crashed_tests(items, results)

        assert len(crashed) == 0
        # Without any crashed tests detected, not_run won't be populated
        assert len(not_run) == 0

    def test_only_not_run_when_crash_detected(self) -> None:
        """Test that not_run is only populated when there's a crashed test."""
        items = [
            self._make_item("test.py::test_one"),
            self._make_item("test.py::test_two"),
        ]
        results = {
            "test.py::test_one": {
                "setup": {"outcome": "passed"},
                "call": {"outcome": "passed"},
                "teardown": {"outcome": "passed"},
            },
            # test_two has no results, but test_one didn't crash
        }

        crashed, not_run = _detect_crashed_tests(items, results)

        # No crashes detected, so not_run should be empty
        assert len(crashed) == 0
        assert len(not_run) == 0
