"""The child launch seam: everything an isolated child subprocess is told.

``plan_launch`` is the single place the child's interface is assembled --
command line, environment, working directory, and the file handle it writes
results to. The run loop plans, runs, and consumes; it never touches option
forwarding, capture policy, env construction, temp-file creation, or cwd
derivation.

Ownership boundary: ``forwarding.child_args`` remains the sole owner of option
policy (what is forwarded/blocked/parent-handled); this module owns argv
packaging around it, plus env, cwd, and the report channel. The child timeout
is a parent-side run parameter, deliberately not part of ``LaunchSpec``.

Spec: issue #65.
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Final, NamedTuple

import pytest

from .config import SUBPROC_ENV, SUBPROC_REPORT_PATH
from .forwarding import child_args

_BASE_CMD: Final = [sys.executable, "-u", "-m", "pytest"]


class LaunchSpec(NamedTuple):
    """The child's entire interface, as an immutable value.

    Mirrors the repo's ``SubprocessResult`` NamedTuple style so tests can
    destructure and compare it field-by-field.
    """

    cmd: list[str]
    env: dict[str, str]
    cwd: str
    report_path: str


def plan_launch(config: pytest.Config, nodeids: list[str]) -> LaunchSpec:
    """Plan an isolated child launch for one group. Pure: no subprocess spawn."""
    capture_mode = config.getoption("capture", "fd")

    # Partial-output invariant: -u (in the base command) forces unbuffered
    # output, and --capture=tee-sys duplicates it to both pytest's capture AND
    # stdout/stderr, so the parent keeps partial output on timeout/crash via
    # subprocess.run(). For normal test completion, output comes through JSONL
    # reports. The parent's --capture setting (not forwarded) controls what the
    # user sees. Exception: if user wants no capture (-s or --capture=no),
    # respect that.
    cmd = [*_BASE_CMD]
    if capture_mode == "no":
        # User wants no capture via -s or --capture=no
        cmd.append("-s")
    else:
        # Default: use tee-sys for timeout/crash output capture
        cmd.append("--capture=tee-sys")
    # Child args and the single --rootdir come from the forwarding interface.
    cmd.extend(child_args(config))
    # The parent-resolved nodeids are the child's positional selectors.
    cmd.extend(nodeids)

    # File where the child will append JSONL records (fresh per plan, i.e. per
    # group; the child writes here because of the env variables below).
    with tempfile.NamedTemporaryFile(
        prefix="pytest-subproc-", suffix=".jsonl", delete=False
    ) as tf:
        report_path = tf.name

    # The child env is the parent's (PYTEST_ADDOPTS included, by inheritance
    # only — no re-emission) plus the two plugin subprocess variables.
    env = os.environ.copy()
    env[SUBPROC_ENV] = "1"
    env[SUBPROC_REPORT_PATH] = report_path

    # Use rootpath if set, otherwise the invocation directory. This ensures
    # nodeids (which are relative to rootpath) can be resolved in the child.
    # Distinct from the --rootdir child argument; this is the only place cwd
    # is decided.
    cwd = str(config.rootpath) if config.rootpath else str(config.invocation_params.dir)

    return LaunchSpec(cmd=cmd, env=env, cwd=cwd, report_path=report_path)
