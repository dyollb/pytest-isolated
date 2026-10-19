# pytest-isolated

[![Tests](https://github.com/dyollb/pytest-isolated/actions/workflows/test.yml/badge.svg)](https://github.com/dyollb/pytest-isolated/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pytest-isolated.svg)](https://pypi.org/project/pytest-isolated/)
[![Python Version](https://img.shields.io/pypi/pyversions/pytest-isolated.svg)](https://pypi.org/project/pytest-isolated/)
[![Downloads](https://static.pepy.tech/badge/pytest-isolated/month)](https://pepy.tech/project/pytest-isolated)
[![License](https://img.shields.io/pypi/l/pytest-isolated.svg)](https://github.com/dyollb/pytest-isolated/blob/main/LICENSE)
[![Platform](https://img.shields.io/badge/platform-windows%20%7C%20linux%20%7C%20macos-lightgrey.svg)](https://github.com/dyollb/pytest-isolated)

A cross-platform pytest plugin that runs marked tests in isolated subprocesses with intelligent grouping.

## Why pytest-isolated?

Ever had a test that passes alone but fails in the suite? Or tests that mysteriously hang on CI? pytest-isolated solves this by running tests in completely isolated subprocesses.

**Common problems it solves:**

- ✅ Segfaults and crashes don't kill your entire test suite
- ✅ Tests modifying global state don't affect each other
- ✅ Hanging tests timeout without blocking other tests
- ✅ C extension crashes are contained
- ✅ Resource leaks are isolated per test

## Cheatsheet for pytest-forked users

This plugin is inspired by [pytest-forked](https://github.com/pytest-dev/pytest-forked). See [pytest-forked migration guide](docs/pytest-forked-migration.md) for a quick reference comparing features.

## Quick Start

**1. Install:**

```bash
pip install pytest-isolated
```

**2. Mark a flaky test:**

```py
# Example 1: Simple isolation
@pytest.mark.isolated
def test_with_clean_state():
    import os

    os.environ["DEBUG"] = "true"
    # Other tests won't see this change


# Example 2: Crash protection
@pytest.mark.isolated
def test_that_crashes():
    import ctypes

    ctypes.string_at(0)  # Crash is contained!
```

**3. Run pytest normally:**

```bash
pytest  # Crash is isolated, suite continues
```

## Features

- Run tests in fresh Python subprocesses to prevent state pollution
- Group related tests to run together in the same subprocess
- Run up to N Group subprocesses in parallel (`--isolated-numprocesses`)
- Handles crashes, timeouts, and setup/teardown failures
- Respects pytest's standard output capture settings (`-s`, `--capture`)
- Works with pytest reporters (JUnit XML, etc.)
- Configurable timeouts to prevent hanging subprocesses
- Cross-platform: Linux, macOS, Windows

## Basic Usage

Mark tests to run in isolated subprocesses:

```python
import pytest


@pytest.mark.isolated
def test_isolated():
    # Runs in a fresh subprocess
    assert True
```

Tests with the same group run together in one subprocess:

```python
# Using keyword argument
@pytest.mark.isolated(group="mygroup")
def test_one():
    shared_state.append(1)


@pytest.mark.isolated(group="mygroup")
def test_two():
    # Sees state from test_one
    assert len(shared_state) == 2


# Or using positional argument
@pytest.mark.isolated("mygroup")
def test_three():
    shared_state.append(3)
```

Set timeout per test group:

```python
@pytest.mark.isolated(timeout=30)
def test_with_timeout():
    # This group gets 30 second timeout (overrides global setting)
    expensive_operation()
```

**Note:** Tests without an explicit `group` parameter each run in their own unique subprocess for maximum isolation.

### Class and Module Markers

Apply to entire classes to share state between methods:

```python
@pytest.mark.isolated
class TestDatabase:
    def test_setup(self):
        self.db = create_database()

    def test_query(self):
        # Shares state with test_setup
        result = self.db.query("SELECT 1")
        assert result
```

Apply to entire modules using `pytestmark`:

```python
import pytest

pytestmark = pytest.mark.isolated


def test_one():
    # Runs in isolated subprocess
    pass


def test_two():
    # Shares subprocess with test_one
    pass
```

### Marker Precedence

When `@pytest.mark.isolated` appears at multiple scopes, the **closest marker wins** (following pytest's standard `get_closest_marker` convention):

1. **Explicit `group` always wins.** `@pytest.mark.isolated(group="name")` uses that group regardless of scope.
1. **Function > class > module.** A function-level `@pytest.mark.isolated` breaks out of any class or module group into its own subprocess.
1. **Class > module.** A class marker groups its methods together, even inside a module with `pytestmark`.

## Configuration

### Command Line

```bash
# Run all tests in isolation (even without @pytest.mark.isolated)
pytest --isolated

# Set isolated test timeout (seconds)
pytest --isolated-timeout=60

# Disable subprocess isolation for debugging
pytest --no-isolation

# Promote Parent/Child contract warnings to errors
pytest --isolated-strict

# Control output capture (standard pytest flags work with isolated tests)
pytest -s                    # Disable capture, show all output
pytest --capture=sys         # Capture at sys.stdout/stderr level

# Combine with pytest debugger
pytest --no-isolation --pdb
```

### pytest.ini / pyproject.toml

```ini
[pytest]
isolated_timeout = 300
isolated_numprocesses = auto
isolated_strict = true
```

Or in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
isolated_timeout = "300"
isolated_numprocesses = "auto"
isolated_strict = true
```

### Parallel Execution

By default each Isolated Group runs in its own subprocess, but only one subprocess
runs at a time. To run Groups concurrently:

```bash
# Up to 8 Group subprocesses at once
pytest --isolated --isolated-numprocesses=8

# One per usable CPU (affinity-aware)
pytest --isolated --isolated-numprocesses=auto
```

- The default (flag absent, `isolated_numprocesses` unset) is serial: results and
  output are byte-identical to earlier releases.
- Outcomes, durations, exit codes, and reporter files (JUnit XML etc.) are
  unchanged by parallelism; only the wall clock moves. Group output arrives as one
  burst when each Child finishes, so `-k`/report ordering is preserved while live
  stdout may appear in completion order.
- Groups that must not overlap anything get `@pytest.mark.isolated(serial=True)`:
  the marker propagates to the whole Group, which then runs alone — nothing else
  launches while it does — and keeps its place in collection order.
- With `--isolated-numprocesses` > 1, each Child also gets its own `tmp_path`
  root and its own fresh pytest cache directory (both under the run's
  `--basetemp`, when given), so concurrent Children never race on shared state —
  while `--lf`/`--ff` inherited via `PYTEST_ADDOPTS`/`addopts` keep working.
- Caution: with `-s`/`--capture=no`, Children write straight to the Parent's
  terminal (live, like plain pytest); under parallelism those streams interleave
  and a one-time warning is issued. Test outcomes and reporters stay correct.

#### Switching from pytest-xdist

| pytest-xdist                       | pytest-isolated                                             |
| ---------------------------------- | ----------------------------------------------------------- |
| `-n 4`                             | `--isolated-numprocesses=4`                                 |
| `@pytest.mark.xdist_group("name")` | `@pytest.mark.isolated(group="name")`                       |
| `-n 4 -x` (drain)                  | same: in-flight Groups finish, unstarted ones do not launch |

Unlike xdist, every Child is a complete fresh pytest process for one Group — no
controller/worker protocol to break on Windows — at the cost of one pytest startup
per Group rather than per worker.

## Fixtures: Parent vs Children

There are two fixture worlds per run. Each Child is a fresh pytest session and
runs the fixtures its Group's tests need — exactly what pytest has always done.
The Parent keeps the session-scoped world: before the first Child launches it
sets up every `scope="session"` autouse conftest fixture (plus their
fixture dependencies), and it tears them down at loop end — so
"start a service for the whole run" works even when every test is isolated
([issue #75](https://github.com/dyollb/pytest-isolated/issues/75)). Module-
and class-scoped fixtures belong to the Child that owns those tests; the
Parent does not run them.

A session fixture that is *not* autouse but is requested by an isolated test
runs once per Child, not once per session (fixture values cannot cross a
process boundary). pytest-isolated warns about this; `--isolated-strict` (or
ini `isolated_strict = true`) turns the warning into an error.

Parent-side setup requires **pytest >= 8** (it rides pytest 8's fixture
internals). On pytest 7 the plugin keeps its pre-0.7 behavior — session
autouse fixtures do not run in the Parent — and says so with a warning.

> **Upgrade note (0.7.1).** If you worked around issue #75 by copying your
> session-fixture logic into `pytest_sessionstart`/`pytest_sessionfinish` and
> kept the original `scope="session"` autouse fixture, both halves now run in
> the Parent — delete one copy. If you *moved* the logic out of the fixture,
> nothing changes for you.

## Real-World Use Cases

### 🌍 Isolating Global State

Prevent environment variable and configuration changes from leaking between tests:

```python
@pytest.mark.isolated
def test_modifies_environ():
    import os

    os.environ["MY_VAR"] = "value"
    # Won't affect other tests


@pytest.mark.isolated
def test_clean_environ():
    import os

    assert "MY_VAR" not in os.environ  # Fresh environment
```

### 🔄 Testing Singletons

Group tests that share singleton state, or isolate them completely:

```python
@pytest.mark.isolated(group="singleton_tests")
def test_singleton_init():
    from myapp import DatabaseConnection

    db = DatabaseConnection.get_instance()
    assert db is not None


@pytest.mark.isolated(group="singleton_tests")
def test_singleton_reuse():
    db = DatabaseConnection.get_instance()
    # Same instance as previous test in group
```

### ⚙️ Testing Process Resources

Safely modify signal handlers and other process-level settings:

```python
@pytest.mark.isolated
def test_signal_handlers():
    import signal

    signal.signal(signal.SIGTERM, custom_handler)
    # Won't interfere with pytest or other tests
```

### 💾 Testing Database Migrations

Group expensive operations while maintaining isolation from other tests:

```python
@pytest.mark.isolated(group="database")
class TestDatabase:
    def test_migration(self):
        db.migrate()  # Expensive operation, runs once

    def test_query(self):
        # Reuses same DB from test_migration
        result = db.query("SELECT 1")
        assert result == [(1,)]
```

### 🔧 Testing C Extensions

Isolate tests that might crash from C code:

```python
@pytest.mark.isolated
def test_numpy_operation():
    import numpy as np

    # If this segfaults, other tests still run
    result = np.array([1, 2, 3])
    assert len(result) == 3
```

## Output and Reporting

Failed tests automatically capture and display stdout/stderr:

```python
@pytest.mark.isolated
def test_failing():
    print("Debug info")
    assert False
```

Works with standard pytest reporters:

```bash
pytest --junitxml=report.xml --durations=10
```

## Limitations

**Fixtures**: Module/session fixtures run in each subprocess group. Cannot share fixture objects between parent and subprocess.

**Debugging**: Use `--no-isolation` to run all tests in the main process for easier debugging with `pdb` or IDE debuggers.

**Performance**: Subprocess creation adds ~100-500ms per group. Group related tests to minimize overhead. Only mark tests that need isolation.

### CLI Option Compatibility

pytest-isolated uses a forward-by-default (blacklist) model:

- Some options are blocked with a clear `UsageError` when isolation is active.
- Some options are supported but handled in the parent process (not forwarded).
- All remaining options (including custom plugin options) are forwarded to children.

See [Incompatible options reference](docs/incompatible-options.md) for the full list and workarounds.

## Advanced

### Coverage Integration

To collect coverage from isolated tests, enable subprocess tracking in `pyproject.toml`:

```toml
[tool.coverage.run]
parallel = true
concurrency = ["subprocess"]
```

See the [coverage.py subprocess documentation](https://coverage.readthedocs.io/en/latest/subprocess.html) for details.

### Timeout Handling

```bash
pytest --isolated-timeout=30
```

Timeout errors are clearly reported with the group name and timeout duration.

### Crash Detection

If a subprocess crashes, tests in that group are marked as failed with exit code information.

### Subprocess Detection

```python
import os

if os.environ.get("PYTEST_RUNNING_IN_SUBPROCESS") == "1":
    # Running in subprocess
    pass
```

## Troubleshooting

**Tests timing out**: Increase timeout with `--isolated-timeout=600`

**Interleaved output with `-s`**: With capture disabled, Children write straight to the
Parent's terminal (as with plain pytest), so concurrent Children interleave when
`--isolated-numprocesses` > 1. Nothing is captured for per-test reports in that mode —
drop `-s` if you want output attached to failures, or drop to serial
(`--isolated-numprocesses=1`) if you need strictly ordered live output.

**Missing output**: Use `-s` or `--capture=no` to see output from passing tests, or `-v` for verbose output. pytest-isolated respects pytest's standard capture settings.

**Subprocess crashes**: Check for segfaults, OOM, or signal issues. Run with `-v` for details.

## Contributing

1. Install pre-commit: `pip install pre-commit && pre-commit install`
1. Run tests: `pytest tests/ -v`
1. Open an issue before submitting PRs for new features

## License

MIT License - see LICENSE file for details.
