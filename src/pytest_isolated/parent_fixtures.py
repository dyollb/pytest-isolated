"""Parent-side session-fixture setup for pytest-isolated.

pytest-isolated replaces ``pytest_runtestloop``, so Isolated Items never go
through the Item setup path that normally instantiates the session fixture
stack; with every test Isolated, session-scoped fixtures never ran in the
Parent (https://github.com/dyollb/pytest-isolated/issues/75, spec
https://github.com/dyollb/pytest-isolated/issues/77). The loop calls the
functions here to set the stack up before the first Child and drain it at
loop end.

This module is the containment boundary for pytest fixture internals: every
``_pytest.*`` touch the Parent-fixture mechanism needs lives here, behind
runtime guards that degrade to the pre-#75 behavior with a warning on
internals drift, so ``execution.py`` only wires hooks to calls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final, Literal, cast

import pytest
from _pytest import skipping
from _pytest.fixtures import FixtureDef, FixtureRequest, FuncFixtureInfo, Scope
from _pytest.nodes import Node
from _pytest.outcomes import Exit, Skipped, XFailed

from .reporting import _emit_report

_PER_CHILD_SESSION_FIXTURE_WARNING: Final = (
    "pytest-isolated: session-scoped fixture {name!r} is requested by "
    "Isolated tests; it runs once per Child process, not once per session "
    "in the Parent. Make it autouse to have the Parent set it up too, or "
    "move per-run state to pytest_sessionstart/pytest_sessionfinish."
)

# The Parent fixture mechanism rides pytest 8 fixture internals: the
# FixtureRequest constructor kwargs and the Node-based
# getfixturedefs/_getautousenames lookups do not exist on pytest 7.
_PARENT_FIXTURES_PYTEST_MIN_MAJOR: Final = 8

_PARENT_FIXTURES_PYTEST_VERSION_WARNING: Final = (
    "pytest-isolated: setting up session-scoped fixtures in the Parent "
    f"requires pytest>={_PARENT_FIXTURES_PYTEST_MIN_MAJOR} (found "
    "{version}); this run keeps the pre-#75 behavior where session "
    "autouse fixtures do not run in the Parent. Upgrade pytest to enable "
    "it (https://github.com/dyollb/pytest-isolated/issues/75)."
)

_SKIPPING_DRIFT_WARNING: Final = (
    "pytest-isolated: could not detect pre-skipped Items "
    "(_pytest.skipping evaluation helpers are unavailable); session "
    "fixtures may be set up for Items that never run. Report against "
    "pytest-isolated."
)


def _pytest_supports_parent_fixtures(version: str) -> bool:
    """Whether this pytest version supports the Parent fixture mechanism."""
    major = version.partition(".")[0]
    try:
        return int(major) >= _PARENT_FIXTURES_PYTEST_MIN_MAJOR
    except ValueError:  # unrecognized version: assume a modern pytest
        return True


def _item_fixture_defs(
    item: pytest.Item,
) -> dict[str, Sequence[FixtureDef[Any]]]:
    fixture_info: FuncFixtureInfo | None = getattr(item, "_fixtureinfo", None)
    return fixture_info.name2fixturedefs if fixture_info is not None else {}


def _fixture_chain(item: pytest.Item, name: str) -> Sequence[FixtureDef[Any]]:
    """The chain for ``name`` visible to this Item, farthest definition first."""
    # The Item's own map holds the full closure for its node, so a
    # subdirectory override of a session fixture resolves here (and
    # direct parametrization entries shadow the manager chain entirely).
    chain = _item_fixture_defs(item).get(name)
    if chain is None:
        chain = item.session._fixturemanager.getfixturedefs(name, item)
    return chain or ()


def _effective_fixture_def(item: pytest.Item, name: str) -> FixtureDef[Any] | None:
    chain = _fixture_chain(item, name)
    return chain[-1] if chain else None


def _live_fixture_defs(item: pytest.Item, name: str) -> list[FixtureDef[Any]]:
    """The chain entries ``name`` resolves through for this Item.

    A same-name override that requests its own name (``def resource(resource)``)
    chains to the entry below it at setup time -- the index walk mirrors
    pytest's closure traversal -- so a wrapper can hide a cold session base
    from a name-only check. An override that does not request its own name
    leaves the entries below it dead for this Item.
    """
    chain = _fixture_chain(item, name)
    live: list[FixtureDef[Any]] = []
    index = -1
    while chain and -index <= len(chain):
        fixture_def = chain[index]
        live.append(fixture_def)
        if name not in fixture_def.argnames:
            break
        index -= 1
    return live


def _effective_scope(item: pytest.Item, fixture_def: FixtureDef[Any]) -> Scope:
    """The scope pytest will anchor this fixture's finalizers at for this Item.

    ``parametrize(..., scope=...)`` overrides the declared scope per Item
    through the callspec, and pytest passes that scope -- not the declared
    one -- to the SubRequest that runs the fixture. A fixture narrowed below
    session must not be set up from the Parent (its function-anchored
    finalizer would assert in SetupState: the Parent has no Item on its
    stack); one widened to session is a session fixture for these Items.
    """
    callspec = getattr(item, "callspec", None)
    arg2scope = getattr(callspec, "_arg2scope", None)
    if arg2scope is not None:
        params = getattr(callspec, "params", None)
        argname = fixture_def.argname
        if params is not None and argname in params:
            scope = arg2scope[argname]
            if scope is not None:
                return cast("Scope", scope)
    return fixture_def._scope


_skipping_drift_warned: Final = {"warned": False}


def _warn_skipping_drift(config: pytest.Config) -> None:
    if not _skipping_drift_warned["warned"]:
        _skipping_drift_warned["warned"] = True
        config.issue_config_time_warning(
            UserWarning(_SKIPPING_DRIFT_WARNING), stacklevel=1
        )


def _item_reaches_fixture_setup(item: pytest.Item) -> bool:
    """Whether stock pytest would run this Item's fixture setup.

    ``pytest_runtest_setup`` in _pytest.skipping is tryfirst and raises the
    skip/[NOTRUN] outcome before the SetupState setup call, so skipped and
    xfail(run=False) Items never instantiate their fixtures -- unless
    --runxfail re-enables the latter.
    """
    evaluate_skip_marks = getattr(skipping, "evaluate_skip_marks", None)
    evaluate_xfail_marks = getattr(skipping, "evaluate_xfail_marks", None)
    if evaluate_skip_marks is None or evaluate_xfail_marks is None:
        _warn_skipping_drift(item.config)
        return True
    if evaluate_skip_marks(item) is not None:
        return False
    xfail_mark = evaluate_xfail_marks(item)
    runxfail = bool(getattr(item.config.option, "runxfail", False))
    return xfail_mark is None or xfail_mark.run or runxfail


class _SessionAnchoredRequest(FixtureRequest):
    """A FixtureRequest whose node is the Session.

    The Parent never runs Item setup, so a function-anchored request
    (TopRequest) reads to pytest's guards as a getfixturevalue during
    teardown: deprecation today, planned hard failure. The Session node is
    active on the SetupState stack (see _setup_parent_session_fixtures).
    """

    def __init__(self, pyfuncitem: pytest.Item) -> None:
        super().__init__(
            pyfuncitem=cast("Any", pyfuncitem),
            fixturename=None,
            arg2fixturedefs=_item_fixture_defs(pyfuncitem).copy(),
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
        # validity is enforced per-request by pytest's own SubRequest logic;
        # callers filter to effective session scope before requesting.
        return None

    @property
    def node(self) -> Node:
        return self._pyfuncitem.session

    def _fillfixtures(self) -> None:
        raise NotImplementedError

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self.node.addfinalizer(finalizer)


def _warn_parent_setup_unavailable(config: pytest.Config, why: str) -> None:
    config.issue_config_time_warning(
        UserWarning(
            "pytest-isolated: could not set up session-scoped fixtures in "
            f"the Parent ({why}); session autouse fixtures may not run "
            "before isolated groups. Report against pytest-isolated."
        ),
        stacklevel=1,
    )


def _setup_parent_session_fixtures(
    session: pytest.Session, items: list[pytest.Item]
) -> None:
    """Set up the session-scope autouse fixture stack in the Parent.

    The Session node is pushed onto the SetupState stack so finalizers
    pytest's own machinery registers land there and run at
    teardown_exact(None); FixtureDef caching keeps every body once-only.
    Fixture bodies and their errors run through pytest's normal
    FixtureDef.execute() path -- setup errors propagate to the caller.

    Each autouse name is requested once per (Item, name) on purpose: the
    request resolves the fixture closure through that Item's own map, so a
    same-name session override in a subdirectory warms its own dependencies
    even when the fixture itself is already cached (stock
    TopRequest._fillfixtures performs the same per-Item walk).
    """
    config = session.config
    fixture_manager = session._fixturemanager
    get_autousenames = getattr(fixture_manager, "_getautousenames", None)
    setup_state = getattr(session, "_setupstate", None)
    if get_autousenames is None or setup_state is None:
        _warn_parent_setup_unavailable(config, "pytest fixture internals")
        return
    stack = getattr(setup_state, "stack", None)
    try:
        if stack is not None and session not in stack:
            stack[session] = ([session.teardown], None)
    except TypeError as exc:  # pragma: no cover - defensive against drift
        _warn_parent_setup_unavailable(config, repr(exc))
        return

    for item in items:
        if not _item_reaches_fixture_setup(item):
            continue
        for name in get_autousenames(item):
            fixture_def = _effective_fixture_def(item, name)
            if fixture_def is None:
                continue
            if _effective_scope(item, fixture_def) is not Scope.Session:
                continue
            _SessionAnchoredRequest(item).getfixturevalue(name)


def _check_per_child_session_fixtures(
    session: pytest.Session, items: list[pytest.Item]
) -> None:
    """Flag session fixtures that only Children will run.

    Called after the Parent stack is set up: a session fixture still cold
    for the Items that reach setup was not executed in the Parent, so it
    will run once per Child instead of once per session.
    """
    config = session.config
    strict = bool(config.getoption("isolated_strict", False)) or bool(
        config.getini("isolated_strict")
    )
    seen: set[int] = set()
    names: list[str] = []
    for item in items:
        if not _item_reaches_fixture_setup(item):
            continue
        for name in getattr(item, "fixturenames", ()):
            for fixture_def in _live_fixture_defs(item, name):
                if _effective_scope(item, fixture_def) is not Scope.Session:
                    continue
                if fixture_def.cached_result is not None:
                    continue
                # pytest's own session fixtures (tmp_path_factory...) are
                # per-Child by design; only user fixtures are the trap
                module = getattr(fixture_def.func, "__module__", "")
                if module.partition(".")[0] == "_pytest":
                    continue
                # Deduplicate on the effective FixtureDef, and only among
                # qualifying defs, so an unqualified Item cannot shadow a
                # later Item's genuinely cold fixture.
                if id(fixture_def) in seen:
                    continue
                seen.add(id(fixture_def))
                names.append(name)
    if not names:
        return
    messages = [_PER_CHILD_SESSION_FIXTURE_WARNING.format(name=name) for name in names]
    if strict:
        raise pytest.UsageError("\n".join(messages))
    for message in messages:
        try:
            config.issue_config_time_warning(UserWarning(message), stacklevel=1)
        except UserWarning as exc:
            # filterwarnings = error escalates the warning; surface it as
            # the usage error --isolated-strict raises, not as an
            # INTERNALERROR traceback from the run loop.
            raise pytest.UsageError(str(exc)) from None


def _report_parent_fixture_outcome(
    session: pytest.Session,
    items: list[pytest.Item],
    excinfo: pytest.ExceptionInfo[BaseException],
    *,
    when: Literal["setup", "teardown"],
) -> None:
    """Report a Parent fixture outcome for Items that never ran their protocol.

    Outcome exceptions (fail/skip/xfail) derive from BaseException, so they
    reach callers of getfixturevalue and teardown_exact just like user
    exceptions do; each maps to its phase-report semantics, everything else
    (including teardown_exact's BaseExceptionGroup of finalizer errors) is a
    failure.
    """
    if excinfo.errisinstance(Skipped):
        reason = str(excinfo.value) or "skipped (no reason given)"
        for it in items:
            _emit_report(it, when=when, outcome="skipped", longrepr=reason)
        return
    if excinfo.errisinstance(XFailed):
        reason = str(excinfo.value) or "xfail (no reason given)"
        for it in items:
            _emit_report(
                it,
                when=when,
                outcome="skipped",
                wasxfail=reason,
            )
        return
    longrepr = excinfo.getrepr(style="long")
    for it in items:
        _emit_report(it, when=when, outcome="failed", longrepr=longrepr)
        session.testsfailed += 1


def _teardown_parent_session_fixtures(
    session: pytest.Session, items: list[pytest.Item]
) -> None:
    """Drain the SetupState stack at loop end.

    Stock pytest drains it inside the loop (the last Item's teardown), which
    lands session fixture teardown before pytest_sessionfinish hooks; with
    no in-process Items the loop must drain it itself. Idempotent when the
    in-process protocol already drained everything. Interrupts and
    pytest.exit() keep propagating; finalizer failures (including outcome
    exceptions and grouped ones) become teardown reports.
    """
    try:
        session._setupstate.teardown_exact(None)
    except (KeyboardInterrupt, Exit):
        raise
    except BaseException:
        _report_parent_fixture_outcome(
            session,
            items,
            pytest.ExceptionInfo.from_current(),
            when="teardown",
        )
