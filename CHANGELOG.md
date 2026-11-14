# Changelog

## Unreleased

- Warn once in the Parent when runnable Isolated tests need unshared session
  fixtures, including autouse fixtures. Per-Child execution is unchanged; use
  Shared Fixtures or Parent lifecycle hooks for run-wide setup and teardown.

## 0.7.0

- New: **Shared Fixtures**
  ([issue #75](https://github.com/dyollb/pytest-isolated/issues/75)). A
  session fixture marked with `@pytest_isolated.shared`, or named in the new
  `isolated_shared_fixtures` ini, runs once in the Parent before the first
  Child launches. Children receive its JSON value and never run the body or
  the dependencies reachable only through it. Teardown runs once in the Parent,
  before `pytest_sessionfinish` hooks, including runs stopped early by
  `--maxfail`/`--exitfirst`.
- A shared fixture that fails, skips, or xfails in the Parent produces that
  outcome only for the tests using it, as in plain pytest. A Parent teardown
  error is reported once. Invalid shared fixtures (not session-scoped,
  parametrized, or a non-JSON value) raise a usage error before any Child
  launches.
- With Shared Fixtures and an explicit `--basetemp`, serial Children get
  separate per-Group temporary directories, so a Child cannot delete live
  Parent resources.
- Unchanged: without Shared Fixtures, session fixtures keep running once per
  Child (like pytest-xdist workers), the Parent runs no fixtures for Isolated
  tests, and Child commands are unchanged.
- Shared Fixtures require pytest 8 or later. On pytest 7 they run once per
  Child, with a warning.
- Fix: a reasonless `xfail` relayed from a Child stays classified as xfailed.

### Upgrade note

If you worked around issue #75 with `pytest_sessionstart`/`pytest_sessionfinish`
hooks guarded on `PYTEST_RUNNING_IN_SUBPROCESS`, those hooks keep working. You
can replace them with a `@pytest_isolated.shared` fixture.
