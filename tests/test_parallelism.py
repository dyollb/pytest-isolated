"""End-to-end tests for group-level parallelism (spec issue #70).

Whole-Parent behavior via pytester, following the patterns in
tests/test_execution.py and tests/test_options.py: outcomes, wall-clock
bounds with CI-safe margins, side-effect files as launch evidence, and
timestamp files for exclusivity. Nothing here inspects plugin internals.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest
from pytest import Pytester


def _module_isolated(name: str, body: str, marker: str = "isolated") -> str:
    """Module-scope isolation = one Group per file."""
    return (
        "import pytest, time, json\nfrom pathlib import Path\n"
        f"pytestmark = pytest.mark.{marker}\n" + textwrap.dedent(body)
    )


def _reported_timed_out(stdout: str, test_name: str) -> bool:
    """True when a report names this test and calls a timeout right after it.

    Windowed (not per-line): pytest wraps summary lines to the terminal
    width, so the nodeid and the verdict can land on separate lines.
    """
    start = stdout.find(test_name)
    while start != -1:
        if "timed out" in stdout[start : start + 200]:
            return True
        start = stdout.find(test_name, start + 1)
    return False


class TestParallelSpeedup:
    def test_concurrent_children_beat_the_serial_bound(
        self, pytester: Pytester
    ) -> None:
        # Four groups, one second each: serial pays four Child startups plus
        # four sleeps; the pool pays one of each. Margin is CI-safe.
        for i in range(4):
            pytester.makepyfile(
                **{
                    f"test_slow_{i}": _module_isolated(
                        f"test_{i}",
                        """
                        def test_sleep():
                            time.sleep(1)
                        """,
                    )
                }
            )

        serial_start = time.monotonic()
        result = pytester.runpytest("-q")
        serial_elapsed = time.monotonic() - serial_start
        result.assert_outcomes(passed=4)

        parallel_start = time.monotonic()
        result = pytester.runpytest("-q", "--isolated-numprocesses=4")
        parallel_elapsed = time.monotonic() - parallel_start
        result.assert_outcomes(passed=4)

        assert parallel_elapsed < serial_elapsed * 0.7, (
            f"parallel {parallel_elapsed:.1f}s vs serial {serial_elapsed:.1f}s"
        )
        # Absolute sanity bound: serial would be >= ~8s on this fixture.
        assert parallel_elapsed < serial_elapsed - 3.0


class TestOutcomeParity:
    """Mixed outcomes under N=2 match the serial run's outcomes."""

    def test_mixed_outcomes_parallel_matches_serial(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_mixed_a=_module_isolated(
                "a",
                """
                def test_pass():
                    assert True

                def test_fail():
                    assert False
                """,
            ),
            test_mixed_b=_module_isolated(
                "b",
                """
                import pytest

                @pytest.mark.skip(reason="nope")
                def test_skip():
                    pass

                def test_crash():
                    import os
                    os.abort()
                """,
            ),
        )

        result = pytester.runpytest("-q", "--isolated-numprocesses=2")
        # b's crash fails the crashing test and there is no further test in b.
        result.assert_outcomes(passed=1, failed=2, skipped=1)


class TestTimeoutParallel:
    def test_timed_out_group_fails_others_still_pass(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_zzz_slow=_module_isolated(
                "slow",
                """
                def test_sleepy():
                    print("PARALLEL_PARTIAL_MARK")
                    time.sleep(10)
                """,
            ),
            test_aaa_fast=_module_isolated(
                "fast",
                """
                def test_quick():
                    assert True
                """,
            ),
        )
        result = pytester.runpytest(
            "-v", "--isolated-timeout=1", "--isolated-numprocesses=2"
        )
        result.assert_outcomes(passed=1, failed=1)
        stdout = result.stdout.str()
        assert "timed out" in stdout
        # Partial-output invariant survives the pool:
        assert "PARALLEL_PARTIAL_MARK" in stdout


class TestTimeoutIsolation:
    @pytest.mark.skipif(sys.platform == "win32", reason="needs an ignorable SIGTERM")
    def test_stubborn_child_cannot_stall_sibling_timeout(
        self, pytester: Pytester
    ) -> None:
        """A Group whose Child ignores terminate() must not let a sibling
        that overruns its own deadline be reported as passed.

        The blocking kill() let Child A's grace period freeze the sweep; B
        exited cleanly during it and skipped the deadline check.
        """
        # The handler goes inside the test, not module level: a module-level
        # call would also run when the module is *imported*, and SIG_IGN
        # survives fork+exec -- an in-process import would make the whole
        # outer pytest session (and every Child it spawns afterwards) ignore
        # SIGTERM.
        stubborn = (
            "import pytest, signal, time\n"
            "pytestmark = pytest.mark.isolated(timeout=2)\n"
            "def test_stubborn():\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    time.sleep(30)\n"
        )
        # A (deadline ~2s) ignores terminate() and freezes a naive blocking
        # kill loop until ~7s. B's deadline is ~3s but it finishes cleanly
        # ~4.5-5s, inside that freeze: the verdict must be a timeout, not
        # the pass a frozen sweep would reap it as.
        overrun = (
            "import pytest, time\n"
            "pytestmark = pytest.mark.isolated(timeout=3)\n"
            "def test_overruns_own_deadline():\n"
            "    time.sleep(4)\n"
        )
        pytester.makepyfile(test_stub=stubborn, test_norm=overrun)

        result = pytester.runpytest_subprocess("-q", "--isolated-numprocesses=2")

        result.assert_outcomes(failed=2)
        stdout = result.stdout.str()
        # Causal per-Group: each Group's own report must carry the timeout
        # verdict, not just some timeout somewhere.
        assert _reported_timed_out(stdout, "test_stubborn")
        assert _reported_timed_out(stdout, "test_overruns_own_deadline")

class TestDrainIsolation:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="needs a POSIX grandchild inheriting the Child's pipe fds",
    )
    def test_drain_freeze_cannot_misreport_a_sibling(
        self, pytester: Pytester
    ) -> None:
        """A finished Child whose pipes a grandchild holds open must not be
        able to freeze the sweep mid-finish() and have a sibling that
        overran its deadline reaped as passed.

        The reap used to join the reader threads inline: with a grandchild
        holding stdout, finish() blocked for its full lifetime and no other
        Child was swept meanwhile.
        """
        # Exits right after leaving a grandchild (inheriting stdout/stderr)
        # alive ~5s; its own drain cannot finish before that.
        holder = (
            "import pytest, subprocess, sys\n"
            "pytestmark = pytest.mark.isolated(timeout=30)\n"
            "def test_leaves_pipe_holder():\n"
            "    subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(5)'])\n"
        )
        # Deadline ~3s, exits ~4.5s -- inside the ~5.4s freeze a blocking
        # drain join imposes -- and must still be judged a timeout.
        overrun = (
            "import pytest, time\n"
            "pytestmark = pytest.mark.isolated(timeout=3)\n"
            "def test_overruns_during_freeze():\n"
            "    time.sleep(4)\n"
        )
        pytester.makepyfile(test_a_hold=holder, test_b_norm=overrun)

        result = pytester.runpytest_subprocess("-q", "--isolated-numprocesses=2")

        # The holder Group completes for real (its report is on disk); only
        # the output drain lags. The overrun Group must be the timeout --
        # and named, not just counted, so the holder's own verdict cannot
        # be confused with it.
        result.assert_outcomes(passed=1, failed=1)
        assert _reported_timed_out(result.stdout.str(), "test_overruns_during_freeze")


class TestSerialExclusivity:
    def test_serial_group_runs_alone_and_others_wait_their_turn(
        self, pytester: Pytester
    ) -> None:
        def timed_body(label: str) -> str:
            # One file per writer: no cross-Child write races regardless of
            # whether the (asserted) exclusivity actually holds.
            stamp = pytester.path / f"{label}.json"
            return f"""
            def test_timed():
                t0 = time.time()
                time.sleep(1.0)
                t1 = time.time()
                Path({str(stamp)!r}).write_text(json.dumps([t0, t1]))
            """

        pytester.makepyfile(
            test_1_regular=_module_isolated("r1", timed_body("regular_1")),
            test_2_serial=_module_isolated(
                "s", timed_body("serial"), marker="isolated(serial=True)"
            ),
            test_3_regular=_module_isolated("r3", timed_body("regular_2")),
        )

        result = pytester.runpytest("-q", "--isolated-numprocesses=4")
        result.assert_outcomes(passed=3)

        def interval(label: str) -> tuple[float, float]:
            return tuple(json.loads((pytester.path / f"{label}.json").read_text()))

        s0, s1 = interval("serial")
        r10, r11 = interval("regular_1")
        r20, r21 = interval("regular_2")
        # The serial Group's interval touches neither regular Group's.
        assert s1 <= r10 + 0.2 or s0 >= r11 - 0.2
        assert s1 <= r20 + 0.2 or s0 >= r21 - 0.2
        # FIFO blocking: the regular group collected AFTER the serial group
        # must start only once the serial group is done (no cutting in line).
        assert r20 >= s1 - 0.2


class TestMaxfailDrain:
    def test_x_stops_launching_but_in_flight_children_finish(
        self, pytester: Pytester
    ) -> None:
        never = pytester.path / "c_never_ran"
        pytester.makepyfile(
            test_a_fail=_module_isolated(
                "a",
                """
                def test_fails_fast():
                    assert False
                """,
            ),
            test_b_inflight=_module_isolated(
                "b",
                """
                def test_slow_pass():
                    time.sleep(1.5)
                    assert True
                """,
            ),
            test_c_later=_module_isolated(
                "c",
                f"""
                def test_should_never_launch():
                    Path({str(never)!r}).write_text("ran")
                """,
            ),
        )

        result = pytester.runpytest("-q", "-x", "--isolated-numprocesses=2")
        # a's failure trips maxfail while b is still in flight; b still
        # reports truthfully; c is never launched (xdist drain semantics).
        result.assert_outcomes(passed=1, failed=1)
        assert not never.exists()


class TestBasetempHardening:
    def test_concurrent_children_get_distinct_basetemp(
        self, pytester: Pytester
    ) -> None:
        for name in ("t1", "t2"):
            out = pytester.path / f"dir_{name}.json"
            pytester.makepyfile(
                **{
                    f"test_dir_{name}": _module_isolated(
                        name,
                        f"""
                        def test_record_tmp(tmp_path):
                            Path({str(out)!r}).write_text(str(tmp_path))
                        """,
                    )
                }
            )

        result = pytester.runpytest("-q", "--isolated-numprocesses=2")
        result.assert_outcomes(passed=2)
        t1 = (pytester.path / "dir_t1.json").read_text()
        t2 = (pytester.path / "dir_t2.json").read_text()
        assert t1 != t2
        assert Path(t1).is_dir() and Path(t2).is_dir()

    def test_inproc_tmp_path_does_not_wipe_group_artifacts(
        self, pytester: Pytester
    ) -> None:
        """A user-given --basetemp is rm_rf'd lazily by pytest's factory at
        the first in-process tmp_path; the Parent must resolve that wipe
        before any Child writes, or finished Children's artifacts vanish."""
        bt = pytester.path / "bt"
        pytester.makepyfile(
            test_iso=_module_isolated(
                "iso",
                """
                def test_leave_artifact(tmp_path):
                    (tmp_path / "child_marker.txt").write_text("keep me")
                """,
            ),
            test_inproc="""
            def test_needs_tmp_path(tmp_path):
                assert tmp_path.is_dir()
            """,
        )

        result = pytester.runpytest(
            "-q", "--isolated-numprocesses=2", "--basetemp", str(bt)
        )
        result.assert_outcomes(passed=2)

        groups_root = bt / "isolated-groups"
        assert groups_root.is_dir(), "in-process factory cleared the groups tree"
        assert list(groups_root.rglob("child_marker.txt")), "Child artifact deleted"


class TestInheritedCacheOptions:
    """Cache-owned options (--lf/--ff) reach the Child through sources the
    Parent cannot filter — PYTEST_ADDOPTS and ini addopts. Disabling the
    cacheprovider plugin unregistered them and crashed the Child's parsing;
    the cache must stay available (redirected, not disabled)."""

    _BODY = "def test_passes():\n    assert True\n"

    def test_lf_via_pytest_addopts(self, pytester: Pytester, monkeypatch) -> None:
        pytester.makepyfile(test_c=_module_isolated("c", self._BODY))
        monkeypatch.setenv("PYTEST_ADDOPTS", "--lf")

        result = pytester.runpytest("-q", "--isolated-numprocesses=2")

        result.assert_outcomes(passed=1)

    def test_ff_via_ini_addopts(self, pytester: Pytester) -> None:
        pytester.makepyfile(test_c=_module_isolated("c", self._BODY))
        pytester.makeini("[pytest]\naddopts = --ff\n")

        result = pytester.runpytest("-q", "--isolated-numprocesses=2")

        result.assert_outcomes(passed=1)


class TestInProcessTestsAfterDrain:
    def test_mixed_isolated_and_normal_at_n2(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            """
            import pytest, time

            @pytest.mark.isolated
            def test_isolated():
                time.sleep(0.2)
                assert True

            def test_normal_runs_in_process():
                import os
                assert os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS") is None
            """
        )
        result = pytester.runpytest("-q", "--isolated-numprocesses=2")
        result.assert_outcomes(passed=2)


class TestCaptureDisabledWarning:
    def test_warns_once_when_s_with_parallel(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_w=_module_isolated(
                "w",
                "def test_ok():\n    print('S-MARKER-LIVE')\n    assert True\n",
            )
        )
        result = pytester.runpytest_subprocess("-q", "-s", "--isolated-numprocesses=2")
        result.assert_outcomes(passed=1)
        assert result.stdout.str().count("will interleave") == 1
        # Children inherit the terminal under -s: the print must be visible.
        assert "S-MARKER-LIVE" in result.stdout.str()

    def test_s_output_visible_in_serial_mode(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_w=_module_isolated(
                "w",
                "def test_ok():\n    print('S-MARKER-SERIAL')\n    assert True\n",
            )
        )
        result = pytester.runpytest_subprocess("-q", "-s")
        result.assert_outcomes(passed=1)
        assert "S-MARKER-SERIAL" in result.stdout.str()

    def test_no_warning_without_s(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_w=_module_isolated("w", "def test_ok():\n    assert True\n")
        )
        result = pytester.runpytest("-q", "--isolated-numprocesses=2")
        result.assert_outcomes(passed=1)
        assert "will interleave" not in result.stdout.str()

    def test_no_warning_when_serial(self, pytester: Pytester) -> None:
        pytester.makepyfile(
            test_w=_module_isolated("w", "def test_ok():\n    assert True\n")
        )
        result = pytester.runpytest("-q", "-s")
        result.assert_outcomes(passed=1)
        assert "will interleave" not in result.stdout.str()


class TestIniConfiguration:
    def test_ini_sets_parallelism(self, pytester: Pytester) -> None:
        pytester.makeini(
            """
            [pytest]
            isolated_numprocesses = 2
            """
        )
        for i in range(2):
            pytester.makepyfile(
                **{
                    f"test_ini_{i}": _module_isolated(
                        f"t{i}",
                        """
                        def test_sleep():
                            time.sleep(1)
                        """,
                    )
                }
            )
        start = time.monotonic()
        result = pytester.runpytest("-q")
        elapsed = time.monotonic() - start
        result.assert_outcomes(passed=2)
        # Serial would pay two startups + two sleeps; ini-driven pool ~one.
        assert elapsed < 3.0, f"ini-driven parallelism took {elapsed:.1f}s"
