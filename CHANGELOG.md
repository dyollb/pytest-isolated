# Changelog

## 0.6.1

- Set up Parent session-scoped autouse fixtures and their dependencies before
  the first Child, including in all-Isolated suites. Finalize them at loop end,
  before `pytest_sessionfinish` hooks, on every path that reaches loop end --
  including runs stopped early by `--maxfail`/`--exitfirst`, where unlaunched
  Groups report nothing (xdist shutdown semantics).
  See <https://github.com/dyollb/pytest-isolated/issues/75>.
- Honor Item-local direct-parametrization overrides during Parent setup and
  per-Child fixture checks.
- Resolve the autouse stack through each Item's own fixture closure, so a
  subdirectory override of a session fixture (including same-name wrappers
  such as `def resource(resource)`) is set up for exactly the Items that see
  it.
- Honor `parametrize(..., scope=...)` scope changes per Item: fixtures
  narrowed below session are left to the Children's per-Item setup; fixtures
  widened to session are set up in the Parent and checked as session fixtures.
- Exclude Items that never reach fixture setup (`skip`/`skipif` marks and
  `xfail(run=False)`) from Parent setup and per-Child fixture checks.
- Report Parent fixture failures, skips, xfails, and finalizer errors without
  losing outcome classification. A Parent setup error prevents Child launches
  and normal test execution.
- Warn about requested session fixtures that run only in Children. The warning
  goes through pytest's config-time warning handling, so `filterwarnings` can
  suppress it, and a `filterwarnings = error` escalation surfaces as a usage
  error. The new `--isolated-strict` flag or `isolated_strict` ini setting
  promotes these warnings to errors.
- Keep pytest 7 supported with a warning and the previous Parent-fixture
  behavior. Parent fixture setup and per-Child fixture checks require pytest 8
  or later.
- The Parent fixture mechanism now lives in one module that guards every use
  of pytest fixture internals and degrades to the previous behavior with a
  warning if a symbol moves in a future pytest.
- Protect Parent resources under an explicit `--basetemp` by giving serial
  Children separate temporary directories. Serial cache and last-failed behavior
  stay unchanged.

### Upgrade note

On pytest 8 or later, if you copied Parent lifecycle logic into
`pytest_sessionstart`/`pytest_sessionfinish` while retaining the original session
autouse fixture, both copies now run. Remove one copy to avoid duplicate setup
and teardown. Users who moved the logic out of the fixture are unaffected.
