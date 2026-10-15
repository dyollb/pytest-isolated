"""Unit tests for execution module helper functions.

Arg-forwarding tests live in tests/test_child_args.py (the child_args interface)."""

import json
from pathlib import Path

from pytest_isolated.execution import (
    _parse_results,
)


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
