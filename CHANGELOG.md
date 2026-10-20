# Changelog

## 0.6.1

- Set up Parent session-scoped autouse fixtures and their dependencies before
  the first Child, including in all-Isolated suites. Finalize them at loop end
  on normal completion, before `pytest_sessionfinish` hooks.
  See <https://github.com/dyollb/pytest-isolated/issues/75>.
- Honor Item-local direct-parametrization overrides during Parent setup and
  per-Child fixture checks.
- Report Parent fixture failures, skips, xfails, and finalizer errors without
  losing outcome classification. A Parent setup error prevents Child launches
  and normal test execution.
- Warn about requested session fixtures that run only in Children. The new
  `--isolated-strict` flag or `isolated_strict` ini setting promotes these warnings
  to errors.
- Keep pytest 7 supported with a warning and the previous Parent-fixture
  behavior. Parent fixture setup and per-Child fixture checks require pytest 8
  or later.
- Protect Parent resources under an explicit `--basetemp` by giving serial
  Children separate temporary directories. Serial cache and last-failed behavior
  stay unchanged.

### Upgrade note

On pytest 8 or later, if you copied Parent lifecycle logic into
`pytest_sessionstart`/`pytest_sessionfinish` while retaining the original session
autouse fixture, both copies now run. Remove one copy to avoid duplicate setup
and teardown. Users who moved the logic out of the fixture are unaffected.
