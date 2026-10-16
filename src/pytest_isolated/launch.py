"""The child launch seam: everything an isolated Child is told.

``plan_launch`` is the single place the child's interface is assembled --
command line (including the hardened plan's per-Group basetemp and cache
dir), environment, working directory, and the file handle it writes results
to. The run loop plans, runs, and consumes; it never touches option
forwarding, capture policy, env construction, temp-file creation, cwd
derivation, or slot-to-directory naming.

Ownership boundary: ``forwarding.child_args`` remains the sole owner of option
policy (what is forwarded/blocked/parent-handled); this module owns argv
packaging around it, plus env, cwd, the report channel, and the whole of
parallel hardening (``open_group_temp_root`` opens the pool's temp tree,
``Hardened`` names its per-slot directories). The child timeout is a
parent-side run parameter, deliberately not part of ``LaunchSpec``.

Specs: https://github.com/dyollb/pytest-isolated/issues/65 and
https://github.com/dyollb/pytest-isolated/issues/73.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
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


class GroupTempRoot(NamedTuple):
    """The pool's per-Group temp tree: the root plus its cleanup obligation.

    ``temp_dir`` is non-None only when no path factory owns the tree (see
    ``open_group_temp_root``); the pool cleans it up at exit, after every
    Child has been killed and reaped.
    """

    root: Path
    temp_dir: tempfile.TemporaryDirectory[str] | None


class Hardened(NamedTuple):
    """Inputs of a hardened plan: the pool's temp root, and one Group's slot.

    Slot-to-directory naming (``group-N`` basetemp, ``cache-N`` cache) is
    private to this module; its only interface is the argv it produces.
    """

    root: Path
    slot: int


def open_group_temp_root(config: pytest.Config) -> GroupTempRoot:
    """Open the per-Group temp root for a concurrent pool.

    Spec: https://github.com/dyollb/pytest-isolated/issues/70

    Concurrent Children must not share pytest's basetemp counter. The tree
    lives inside the Parent's resolved basetemp (artifacts stay together and
    pytest's retention rules apply); with the tmpdir plugin disabled there is
    no factory, so a plugin-owned temp dir cleaned at pool exit is used.

    Resolution goes through the Parent's TempPathFactory on purpose: for a
    user-given --basetemp, pytest's factory rm_rf's that directory lazily at
    the first in-process tmp_path. Resolving here performs the wipe before
    any Child writes and caches the result, so the later lazy call can never
    delete finished Children's artifacts from under us.
    """
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        root = Path(factory.getbasetemp()) / "isolated-groups"
        root.mkdir(parents=True, exist_ok=True)
        return GroupTempRoot(root=root, temp_dir=None)
    # tmpdir plugin disabled (e.g. -p no:tmpdir): no factory to resolve
    # through, and the option may still be set — own our cleanup instead.
    tmp = tempfile.TemporaryDirectory(prefix="pytest-isolated-basetemp-")
    return GroupTempRoot(root=Path(tmp.name), temp_dir=tmp)


def plan_launch(
    config: pytest.Config,
    nodeids: list[str],
    *,
    hardened: Hardened | None = None,
) -> LaunchSpec:
    """Plan an isolated child launch for one group. Pure: no subprocess spawn.

    The hardened form (spec: https://github.com/dyollb/pytest-isolated/issues/73)
    is used only by the concurrent pool: slot N of the pool's temp root gets a
    unique ``group-N`` basetemp and a unique ``cache-N`` cache dir, so
    concurrent Children cannot race on pytest's shared basetemp counter or one
    ``.pytest_cache``. The cache is redirected via the ``cache_dir`` ini
    (``-o``, portable since pytest 7) rather than ``-p no:cacheprovider``:
    disabling the plugin also unregisters ``--lf``/``--ff``, so those options
    inherited through ``PYTEST_ADDOPTS`` or ini ``addopts`` crashed the Child's
    argument parsing. With the plugin alive and a fresh per-Group cache, they
    parse and behave as they do in serial runs. Both tokens are appended AFTER
    the forwarded args so an inherited ``--basetemp``/``-o`` cannot override
    them. Serial launches pass no ``hardened`` and get the pre-parallel command
    unchanged.
    """
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
    # Parallel hardening, injected last so they win over forwarded duplicates.
    if hardened is not None:
        cmd.extend(
            [
                "-o",
                f"cache_dir={hardened.root / f'cache-{hardened.slot}'}",
                "--basetemp",
                str(hardened.root / f"group-{hardened.slot}"),
            ]
        )
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
