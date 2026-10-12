"""The parent->child configuration seam: what the child pytest is told.

This module owns the single decision "which arguments does an isolated child
subprocess receive?" and the single enforcement point for options that are
incompatible with isolation.

Design (spec: docs/spec-child-args-forwarding.md, ADR-0001):

- Option value-arity is derived from the parent's parsed pytest parser, never
  from a hand-maintained list. Third-party and conftest-registered options are
  handled correctly with no per-option code.
- One policy table maps option names to a kind: ``blocked`` (rejected with a
  UsageError when isolation is active), ``parent-handled`` (consumed by the
  parent, not forwarded), or implicitly ``forwarded`` (everything else).
- ``PYTEST_ADDOPTS`` needs no special handling: pytest merges it into the
  parsed state and the child inherits the environment variable, so parsed-state
  checks and env inheritance cover both sides of the seam.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum, auto
from typing import Final

import pytest


class OptionKind(Enum):
    """How isolation treats an option."""

    BLOCKED = auto()
    PARENT_HANDLED = auto()


# Blocked options: name -> reason shown in the UsageError. Values are never
# forwarded either (the parser tells us whether the name consumes one).
BLOCKED_OPTIONS: Final[dict[str, str]] = {
    # Interactive/Debugger -- require terminal input, unavailable in subprocess
    "--pdb": "requires interactive debugger access",
    "--pdbcls": "requires interactive debugger access",
    "--trace": "requires interactive debugger access",
    "--full-trace": "tied to interactive exception display",
    # Import/Discovery -- differs between parent and child processes
    "--confcutdir": "conftest search path differs between parent and child",
    "--noconftest": "parent and child need conftest for consistency",
    "--collect-in-virtualenv": "virtualenv detection may differ per process",
    # Caching/State -- depends on per-process state
    "--nf": "file modification times differ between parent and child",
    "--new-first": "file modification times differ between parent and child",
    "--sw": "stepwise state tracking lost across subprocess boundary",
    "--stepwise": "stepwise state tracking lost across subprocess boundary",
    "--sw-skip": "stepwise state tracking lost across subprocess boundary",
    "--sw-reset": "stepwise state tracking lost across subprocess boundary",
    "--cache-show": "shows only parent's cache, not subprocess cache",
    "--cache-clear": "clears parent cache only, subprocess cache unaffected",
    # Configuration -- config file selection must stay parent/child aligned
    "-c": "parent and child must use same config file",
    "--config-file": "parent and child must use same config file",
    # Fixture/Execution -- prevent test execution or affect fixture scope
    "--setup-only": "setup-only prevents test execution in child",
    "--setup-plan": "setup-plan prevents test execution in child",
    # Debug pytest -- incomplete debug info from parent only
    "--trace-config": "traces parent's conftest, not subprocess's",
    "--debug": "subprocess debug output may be lost",
    # xfail handling
    "--runxfail": "xfail handling inconsistent between parent and child",
}

# Consumed by the parent and resolved before the subprocess split. The child
# receives resolved nodeids instead of selectors, and capture/rootdir policy
# is applied when the child command is assembled.
PARENT_HANDLED_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "--co",
        "--collect-only",
        "--pyargs",
        "--keep-duplicates",
        "--fixtures",
        "--funcargs",
        "--fixtures-per-test",
        "--lf",
        "--last-failed",
        "--ff",
        "--failed-first",
        "-s",
        "--capture",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--rootdir",
        "--isolated",
        "--isolated-timeout",
        "--no-isolation",
    }
)

_POLICY: Final[dict[str, OptionKind]] = {
    **dict.fromkeys(BLOCKED_OPTIONS, OptionKind.BLOCKED),
    **dict.fromkeys(PARENT_HANDLED_OPTIONS, OptionKind.PARENT_HANDLED),
}

# Rootdir injection: the parent's resolved rootpath is stated exactly once,
# replacing any user-supplied form.
_ROOTDIR_OPT: Final = "--rootdir"


def _option_string_actions(config: pytest.Config) -> Mapping[str, object]:
    """The parent's parsed argparse action table (option string -> action).

    This is where value-arity comes from: pytest registers every core, plugin,
    and conftest option here before the runtest loop starts.
    """
    try:
        actions: Mapping[str, object] = config._parser.optparser._option_string_actions
    except AttributeError as exc:  # pragma: no cover - unexpected pytest version
        msg = (
            "pytest-isolated could not read the pytest option table; the child "
            "argument interface requires pytest's parsed parser. "
            f"pytest version: {pytest.__version__}"
        )
        raise RuntimeError(msg) from exc
    return actions


def _value_count(action: object) -> int:
    """Maximum number of value tokens the option consumes (0 if it is a flag).

    Mirrors argparse: nargs=None is the store-style "exactly one value"; 0
    covers flags (store_true/store_const/count); '?' is an optional single
    value; '*'/'+' are greedy and reported as -1 (consume while not
    option-like).
    """
    nargs = getattr(action, "nargs", None)
    if nargs is None:
        return 1
    if isinstance(nargs, int):
        return nargs
    if nargs == "?":
        return 1
    if nargs in ("*", "+"):
        return -1
    return 1  # unknown nargs string: be conservative and consume one


# argparse's end-of-options marker: everything after it is positional, even
# option-looking tokens (they are parent-side selectors, never forwarded).
_END_OF_OPTIONS: Final = "--"


def _looks_like_option(token: str) -> bool:
    """Mirror argparse: a token starting with '-' is not a value.

    A bare "-" or a negative number can be a value; keep it simple and treat
    every other dash-leading token as an option (same rule pytest itself uses).
    """
    if token == "-":
        return False
    if not token.startswith("-"):
        return False
    body = token.lstrip("-")
    try:
        float(body)
    except ValueError:
        return True
    return False  # negative number: argparse accepts it as a value


def child_args(config: pytest.Config) -> list[str]:
    """Return the pytest arguments an isolated child subprocess should receive.

    The deep interface behind the parent->child seam: callers know no option
    names, token forms, or scanning rules. Positional selectors are excluded
    (the launcher appends the parent-resolved nodeids); scanning stops at a
    literal ``--`` so everything after it is treated as a selector; blocked and
    parent-handled options are dropped together with any value tokens they
    consume; everything else is forwarded in its original token form with
    arity derived from the parser. The parent's resolved rootpath is appended
    exactly once.
    """
    actions = _option_string_actions(config)
    args = config.invocation_params.args
    forwarded: list[str] = []
    i = 0

    while i < len(args):
        token = args[i]
        if token == _END_OF_OPTIONS:
            break  # parent-side selectors only: the child gets resolved nodeids

        if _looks_like_option(token):
            name, sep, _value = token.partition("=")
            resolved, count = _resolve_option(name, sep, actions)
            kind = _POLICY.get(resolved)

            if kind is not None:
                # Blocked or parent-handled: drop the token and its values.
                i = _skip_values(args, i + 1, count)
                continue

            forwarded.append(token)
            i = _forward_values(args, i + 1, count, forwarded)
            continue

        # Positional selector: the child gets resolved nodeids instead.
        i += 1

    if config.rootpath:
        forwarded.extend([_ROOTDIR_OPT, str(config.rootpath)])
    return forwarded


def _resolve_option(
    name: str, sep: str, actions: Mapping[str, object]
) -> tuple[str, int]:
    """Resolve an option token's name part the way argparse's matcher does.

    Returns (option string the parser and policy tables know, number of
    *following* tokens consumed). ``=``-joined tokens are direct and
    self-contained. A single-dash cluster peels registered zero-arity flags off
    the front (``-qk`` resolves to ``-k``, so ``-qk expr`` keeps ``-k``'s arity
    and forwards the pair with its value); a registered value option reached
    mid-cluster absorbs the cluster's own tail inline (``-kk`` is ``-k='k'``,
    consuming no following token). Unknown tokens stay self-contained -- pytest
    rejects unknown options at parse time, so that branch is defensive only.
    """
    if sep:
        return name, 0  # the joined token carries its own value
    if not name.startswith("--"):
        while len(name) > 2 and actions.get(name) is None:
            head = name[:2]  # argparse peels one short flag at a time
            head_action = actions.get(head)
            if head_action is None:
                break  # unknown head: nothing to peel, the whole token stays
            if _value_count(head_action) != 0:
                return head, 0  # head absorbs the rest of the token inline
            name = f"-{name[2:]}"
    action = actions.get(name)
    return name, 0 if action is None else _value_count(action)


def _skip_values(args: Sequence[str], start: int, count: int) -> int:
    """Index after skipping an option's value tokens at ``start`` (no append)."""
    j, n = start, 0
    while not (0 <= count <= n) and j < len(args):
        if _looks_like_option(args[j]):
            break
        j, n = j + 1, n + 1
    return j


def _forward_values(
    args: Sequence[str], start: int, count: int, forwarded: list[str]
) -> int:
    """Append an option's value tokens at ``start``; return the next index."""
    j, n = start, 0
    while not (0 <= count <= n) and j < len(args):
        if _looks_like_option(args[j]):
            break
        forwarded.append(args[j])
        j, n = j + 1, n + 1
    return j


def validate_isolation_compatibility(config: pytest.Config) -> None:
    """Reject blocked options when isolation is active. Single enforcement point.

    Reads the same policy table as :func:`child_args`, via the *parsed* option
    state -- so options arriving through the command line, ``PYTEST_ADDOPTS``,
    or the ``addopts`` ini all fail identically. Raises ``pytest.UsageError``
    naming the option, its reason, and the ``--no-isolation`` escape hatch.
    """
    actions = _option_string_actions(config)
    triggered_dests: dict[str, str] = {}  # dest -> fallback name from the table

    for name in BLOCKED_OPTIONS:
        action = actions.get(name)
        if action is None:
            continue  # not registered (plugin subset); nothing could have set it
        dest = action.dest  # type: ignore[attr-defined]
        if dest in triggered_dests:
            continue  # alias of an already-checked option (e.g. -c/--config-file)
        default = action.default  # type: ignore[attr-defined]
        if getattr(config.option, dest, default) != default:
            triggered_dests[dest] = name

    if not triggered_dests:
        return

    # Report the option as the user wrote it when possible (an aliased
    # destination like -c/--config-file would otherwise be named by whichever
    # table row comes first).
    typed = _names_as_typed(triggered_dests, config.invocation_params.args, actions)
    for dest, fallback in triggered_dests.items():
        name = typed.get(dest, fallback)
        reason = BLOCKED_OPTIONS[name]
        msg = (
            f"Option '{name}' is incompatible with @pytest.mark.isolated: "
            f"{reason}. Use --no-isolation to disable isolation for this run, "
            f"or remove the option."
        )
        raise pytest.UsageError(msg)


def _names_as_typed(
    dests: Mapping[str, str], args: Sequence[str], actions: Mapping[str, object]
) -> dict[str, str]:
    """Map each triggered dest to the blocked option string that appears in argv.

    Destinations triggered only via PYTEST_ADDOPTS/addopts (never typed as a
    token) stay absent and fall back to the table row's name.
    """
    typed: dict[str, str] = {}
    for token in args:
        name = token.partition("=")[0]
        if name not in BLOCKED_OPTIONS:
            continue
        action = actions.get(name)
        if action is not None:
            dest = getattr(action, "dest", None)
            if isinstance(dest, str) and dest in dests and dest not in typed:
                typed[dest] = name
    return typed
