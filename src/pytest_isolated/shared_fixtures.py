"""Shared Fixtures: session fixtures set up once in the Parent for all Children.

By default every Child is a fresh pytest session that runs the session
fixtures its tests need (like pytest-xdist workers). A fixture opted in with
:func:`shared` (or the ``isolated_shared_fixtures`` ini) instead runs once in
the Parent before the first Child launches; its JSON value is handed to the
Children, which never run the fixture body. Teardown happens once, in the
Parent, at loop end (https://github.com/dyollb/pytest-isolated/issues/75).

This module is the containment boundary for pytest fixture internals: every
``_pytest.*`` touch the Parent side needs lives here, behind runtime guards
that fall back to per-process execution with a warning on internals drift.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple, TypeVar, cast

import pytest
from _pytest import skipping
from _pytest.fixtures import FixtureDef, FixtureRequest, FuncFixtureInfo, Scope
from _pytest.nodes import Node
from _pytest.outcomes import Exit, Skipped, XFailed

from .config import SHARED_PAYLOAD_ENV, SUBPROC_ENV
from .reporting import _emit_report

_F = TypeVar("_F", bound=Callable[..., Any])

_SHARED_ATTR: Final = "_pytest_isolated_shared"

# The Parent side rides pytest 8 fixture internals: the FixtureRequest
# constructor kwargs and the Node-based getfixturedefs/_getautousenames
# lookups do not exist on pytest 7.
_PARENT_FIXTURES_PYTEST_MIN_MAJOR: Final = 8

_PYTEST_VERSION_WARNING: Final = (
    "pytest-isolated: Shared Fixtures require pytest>="
    f"{_PARENT_FIXTURES_PYTEST_MIN_MAJOR} (found {{version}}); shared "
    "fixtures run once per Child process instead."
)

_SKIPPING_DRIFT_WARNING: Final = (
    "pytest-isolated: could not detect pre-skipped Items "
    "(_pytest.skipping evaluation helpers are unavailable); shared fixtures "
    "may be set up for Items that never run. Report against pytest-isolated."
)


def shared(func: _F) -> _F:
    """Run this session fixture once in the Parent and share its value.

    Apply next to ``@pytest.fixture(scope="session")`` (either order). The
    fixture value must be JSON-serializable: Children receive a JSON copy
    and never run the fixture body.
    """
    target: Any = func
    unwrap = getattr(func, "_get_wrapped_function", None)  # pytest >= 8.4
    if callable(unwrap):
        target = unwrap()
    elif hasattr(func, "__pytest_wrapped__"):  # pytest < 8.4
        target = func.__pytest_wrapped__.obj
    setattr(target, _SHARED_ATTR, True)
    if target is not func:
        setattr(func, _SHARED_ATTR, True)
    return func


class _SharedUse(NamedTuple):
    item: pytest.Item
    name: str
    fixture_defs: list[FixtureDef[Any]]


def _pytest_supports_parent_fixtures(version: str) -> bool:
    """Whether this pytest version supports Parent-side fixture setup."""
    major = version.partition(".")[0]
    try:
        return int(major) >= _PARENT_FIXTURES_PYTEST_MIN_MAJOR
    except ValueError:  # unrecognized version: assume a modern pytest
        return True


def _shared_names(config: pytest.Config) -> frozenset[str]:
    return frozenset(config.getini("isolated_shared_fixtures"))


def _is_shared(fixture_def: FixtureDef[Any], names: frozenset[str]) -> bool:
    if fixture_def.argname in names:
        return True
    return bool(getattr(fixture_def.func, _SHARED_ATTR, False))


def _shared_key(fixture_def: FixtureDef[Any]) -> str:
    """Identify a FixtureDef identically in the Parent and in every Child."""
    return f"{fixture_def.baseid}::{fixture_def.argname}"


def _item_fixture_defs(
    item: pytest.Item,
) -> dict[str, Sequence[FixtureDef[Any]]]:
    fixture_info: FuncFixtureInfo | None = getattr(item, "_fixtureinfo", None)
    return dict(fixture_info.name2fixturedefs) if fixture_info is not None else {}


def _live_fixture_defs(item: pytest.Item, name: str) -> list[FixtureDef[Any]]:
    """The chain entries ``name`` resolves through for this Item.

    The Item's own map holds its full closure, so subdirectory overrides and
    direct parametrization resolve here. A same-name override that requests
    its own name (``def resource(resource)``) chains to the entry below it;
    an override that does not leaves the entries below it dead for this Item.
    """
    chain = _item_fixture_defs(item).get(name) or ()
    live: list[FixtureDef[Any]] = []
    for fixture_def in reversed(chain):
        live.append(fixture_def)
        if name not in fixture_def.argnames:
            break
    return live


_skipping_drift_warned: Final = {"warned": False}


def _item_reaches_fixture_setup(item: pytest.Item) -> bool:
    """Whether stock pytest would run this Item's fixture setup.

    ``pytest_runtest_setup`` in _pytest.skipping is tryfirst and raises the
    skip/[NOTRUN] outcome before fixture setup, so skipped and
    xfail(run=False) Items never instantiate their fixtures -- unless
    --runxfail re-enables the latter.
    """
    evaluate_skip_marks = getattr(skipping, "evaluate_skip_marks", None)
    evaluate_xfail_marks = getattr(skipping, "evaluate_xfail_marks", None)
    if evaluate_skip_marks is None or evaluate_xfail_marks is None:
        if not _skipping_drift_warned["warned"]:
            _skipping_drift_warned["warned"] = True
            item.config.issue_config_time_warning(
                UserWarning(_SKIPPING_DRIFT_WARNING), stacklevel=1
            )
        return True
    if evaluate_skip_marks(item) is not None:
        return False
    xfail_mark = evaluate_xfail_marks(item)
    runxfail = bool(getattr(item.config.option, "runxfail", False))
    return xfail_mark is None or xfail_mark.run or runxfail


def _find_shared_uses(
    items: list[pytest.Item], names: frozenset[str]
) -> list[_SharedUse]:
    uses: list[_SharedUse] = []
    for item in items:
        if not _item_reaches_fixture_setup(item):
            continue
        for name in getattr(item, "fixturenames", ()):
            live = _live_fixture_defs(item, name)
            shared_defs = [d for d in live if _is_shared(d, names)]
            if shared_defs:
                uses.append(_SharedUse(item, name, shared_defs))
    return uses


def _warn_per_child_session_fixtures(
    items: list[pytest.Item], names: frozenset[str]
) -> None:
    fixtures: set[str] = set()
    for item in items:
        if not _item_reaches_fixture_setup(item):
            continue
        fixture_info: FuncFixtureInfo | None = getattr(item, "_fixtureinfo", None)
        pending = list(fixture_info.initialnames) if fixture_info is not None else []
        visited: set[str] = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            for fixture_def in _live_fixture_defs(item, name):
                if _is_shared(fixture_def, names):
                    break
                if fixture_def.scope == "session":
                    fixtures.add(_shared_key(fixture_def))
                pending.extend(dep for dep in fixture_def.argnames if dep != name)
    if fixtures:
        _warn(
            items[0].config,
            "pytest-isolated: session fixtures used by Isolated tests "
            "(including autouse) run once per Child, not in the Parent for "
            f"those tests: {', '.join(sorted(fixtures))}. "
            "For Parent-owned setup/teardown, opt in with "
            "@pytest_isolated.shared or isolated_shared_fixtures, or use "
            "pytest_sessionstart/pytest_sessionfinish hooks guarded by "
            "PYTEST_RUNNING_IN_SUBPROCESS and collectonly.",
        )


def _validate_shared_uses(uses: list[_SharedUse]) -> None:
    """Raises pytest.UsageError for fixtures that cannot have one shared value."""
    for use in uses:
        for fixture_def in use.fixture_defs:
            where = f"{fixture_def.argname!r} ({_shared_key(fixture_def)})"
            if fixture_def.scope != "session":
                msg = (
                    f"pytest-isolated: shared fixture {where} must be "
                    f"scope='session', not {fixture_def.scope!r}"
                )
                raise pytest.UsageError(msg)
            callspec = getattr(use.item, "callspec", None)
            parametrized = fixture_def.argname in getattr(callspec, "params", {})
            if fixture_def.params is not None or parametrized:
                msg = (
                    f"pytest-isolated: shared fixture {where} cannot be "
                    "parametrized; one value is shared by every Child"
                )
                raise pytest.UsageError(msg)


class _SessionAnchoredRequest(FixtureRequest):
    """A FixtureRequest whose node is the Session.

    The Parent never runs Item setup, so a function-anchored request
    (TopRequest) reads to pytest's guards as a getfixturevalue during
    teardown: deprecation today, planned hard failure. The Session node is
    active on the SetupState stack (see setup_shared_fixtures).
    """

    def __init__(self, pyfuncitem: pytest.Item) -> None:
        super().__init__(
            pyfuncitem=cast("Any", pyfuncitem),
            fixturename=None,
            arg2fixturedefs=_item_fixture_defs(pyfuncitem),
            fixture_defs={},
            _ispytest=True,
        )

    @property
    def _scope(self) -> Scope:
        return Scope.Session

    def _check_scope(
        self,
        _requested_fixturedef: FixtureDef[object],
        _requested_scope: Scope,
    ) -> None:
        # Mirrors stock TopRequest: no top-level check. Dependency scope
        # validity is enforced per SubRequest.
        return None

    @property
    def node(self) -> Node:
        return self._pyfuncitem.session

    def _fillfixtures(self) -> None:
        raise NotImplementedError

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self.node.addfinalizer(finalizer)


def _warn(config: pytest.Config, message: str) -> None:
    config.issue_config_time_warning(UserWarning(message), stacklevel=1)


def _activate_session(session: pytest.Session) -> bool:
    """Push the Session node on the SetupState stack, as SetupState.setup does."""
    setup_state = getattr(session, "_setupstate", None)
    stack = getattr(setup_state, "stack", None)
    if stack is None:
        return False
    try:
        if session not in stack:
            stack[session] = ([session.teardown], None)
    except TypeError:  # pragma: no cover - defensive against drift
        return False
    return True


def _error_entry(
    fixture_def: FixtureDef[Any], exc: BaseException
) -> dict[str, dict[str, str]]:
    if isinstance(exc, Skipped):
        return {"error": {"kind": "skip", "message": str(exc)}}
    if isinstance(exc, XFailed):
        return {"error": {"kind": "xfail", "message": str(exc)}}
    excinfo = pytest.ExceptionInfo.from_exception(exc)
    message = (
        f"shared fixture {fixture_def.argname!r} failed in the Parent process:\n"
        f"{excinfo.getrepr(style='long')}"
    )
    return {"error": {"kind": "fail", "message": message}}


def setup_shared_fixtures(
    session: pytest.Session, items: list[pytest.Item]
) -> Path | None:
    """Set up the shared fixtures the Isolated Items need; return the payload.

    Returns None when no runnable Item uses a shared fixture (nothing is
    set up, so fixtures without opt-in still run per Child, with a warning).
    Raises pytest.UsageError for invalid shared fixtures and non-JSON values.
    Setup failures and skips do not abort the run: they are recorded in the
    payload and re-raised in each Child that needs the fixture, matching
    stock pytest where only the Items using a failed fixture error.
    """
    config = session.config
    names = _shared_names(config)
    _warn_per_child_session_fixtures(items, names)
    uses = _find_shared_uses(items, names)
    if not uses:
        return None
    _validate_shared_uses(uses)
    if not _pytest_supports_parent_fixtures(pytest.__version__):
        _warn(config, _PYTEST_VERSION_WARNING.format(version=pytest.__version__))
        return None
    if not _activate_session(session):
        _warn(
            config,
            "pytest-isolated: could not set up shared fixtures in the Parent "
            "(pytest fixture internals changed); they run once per Child "
            "process instead. Report against pytest-isolated.",
        )
        return None

    payload: dict[str, dict[str, Any]] = {}
    for use in uses:
        caught: BaseException | None = None
        try:
            _SessionAnchoredRequest(use.item).getfixturevalue(use.name)
        except (KeyboardInterrupt, Exit):
            raise
        except BaseException as exc:
            caught = exc
        for fixture_def in use.fixture_defs:
            key = _shared_key(fixture_def)
            if key in payload:
                continue
            cached = fixture_def.cached_result
            if cached is not None and cached[2] is None:
                payload[key] = {"value": cached[0]}
            elif cached is not None:
                payload[key] = _error_entry(fixture_def, cached[2][0])
            elif caught is not None:
                payload[key] = _error_entry(fixture_def, caught)

    try:
        text = json.dumps(payload)
    except (TypeError, ValueError):
        bad = [
            key.removeprefix("::")
            for key, entry in payload.items()
            if not _json_serializable(entry.get("value"))
        ]
        msg = (
            "pytest-isolated: shared fixture values must be JSON-serializable "
            f"(they are copied into every Child): {', '.join(bad)}"
        )
        raise pytest.UsageError(msg) from None
    fd, name = tempfile.mkstemp(prefix="pytest-isolated-shared-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(name)


def _json_serializable(value: object) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def teardown_shared_fixtures(session: pytest.Session, report_item: pytest.Item) -> None:
    """Drain the SetupState stack at loop end, before pytest_sessionfinish.

    Stock pytest drains it during the last Item's teardown; with no
    in-process Items the loop must drain it itself. Idempotent when the
    in-process protocol already drained everything. A finalizer error is
    reported once, on ``report_item``, like stock pytest attributes session
    teardown errors to the last Item.
    """
    try:
        session._setupstate.teardown_exact(None)
    except (KeyboardInterrupt, Exit):
        raise
    except BaseException:
        excinfo = pytest.ExceptionInfo.from_current()
        _report_teardown_outcome(session, report_item, excinfo)


def _report_teardown_outcome(
    session: pytest.Session,
    item: pytest.Item,
    excinfo: pytest.ExceptionInfo[BaseException],
) -> None:
    when: Literal["teardown"] = "teardown"
    if excinfo.errisinstance(Skipped):
        reason = str(excinfo.value) or "skipped (no reason given)"
        _emit_report(item, when=when, outcome="skipped", longrepr=reason)
        return
    _emit_report(
        item, when=when, outcome="failed", longrepr=excinfo.getrepr(style="long")
    )
    session.testsfailed += 1


def _serve_entry(entry: dict[str, Any]) -> Callable[..., object]:
    """A stand-in fixture function that replays the Parent's outcome."""
    error = entry.get("error")

    def serve(*_args: object, **_kwargs: object) -> object:
        if error is not None:
            kind, message = error["kind"], error["message"]
            if kind == "skip":
                pytest.skip(message)
            if kind == "xfail":
                pytest.xfail(message)
            pytest.fail(message, pytrace=False)
        return entry["value"]

    return serve


def serve_shared_fixtures_in_child(session: pytest.Session) -> None:
    """Replace shared fixture bodies with the Parent's recorded outcomes.

    Each shared FixtureDef gets a stand-in function and no arguments, and
    each Item's closure is pruned, so neither the body nor dependencies
    reachable only through it run in the Child. pytest's own fixture setup
    then caches the value (or outcome) exactly as for a real fixture.
    """
    path = os.environ.get(SHARED_PAYLOAD_ENV)
    if not path:
        return
    with Path(path).open(encoding="utf-8") as f:
        payload: dict[str, dict[str, Any]] = json.load(f)
    served: set[int] = set()
    for item in session.items:
        fixture_info: FuncFixtureInfo | None = getattr(item, "_fixtureinfo", None)
        if fixture_info is None:
            continue
        touched = False
        for chain in fixture_info.name2fixturedefs.values():
            for fixture_def in chain:
                entry = payload.get(_shared_key(fixture_def))
                if entry is None:
                    continue
                touched = True
                if id(fixture_def) not in served:
                    served.add(id(fixture_def))
                    object.__setattr__(fixture_def, "func", _serve_entry(entry))
                    object.__setattr__(fixture_def, "argnames", ())
        if touched:
            fixture_info.prune_dependency_tree()


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    if os.environ.get(SUBPROC_ENV) == "1":
        serve_shared_fixtures_in_child(session)
