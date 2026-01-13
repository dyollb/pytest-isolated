# pytest-isolated

[![Tests](https://github.com/dyollb/pytest-isolated/actions/workflows/test.yml/badge.svg)](https://github.com/dyollb/pytest-isolated/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/pytest-isolated.svg)](https://pypi.org/project/pytest-isolated/)

A pytest plugin that runs marked tests in isolated subprocesses with intelligent grouping.

## Features

- Run tests in fresh Python subprocesses to prevent state pollution
- Group related tests to run together in the same subprocess
- Handles crashes, timeouts, and setup/teardown failures
- Captures stdout/stderr for failed tests
- Works with pytest reporters (JUnit XML, etc.)
- Configurable timeouts to prevent hanging subprocesses
- Cross-platform: Linux, macOS, Windows

## Installation

```bash
pip install pytest-isolated
```

## Quick Start

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
@pytest.mark.isolated(group="mygroup")
def test_one():
    shared_state.append(1)

@pytest.mark.isolated(group="mygroup")
def test_two():
    # Sees state from test_one
    assert len(shared_state) == 2
```

Set timeout per test group:

```python
@pytest.mark.isolated(timeout=30)
def test_with_timeout():
    # This group gets 30 second timeout (overrides global setting)
    expensive_operation()
```

Tests without an explicit group are automatically grouped by module.

## Configuration

### Command Line

```bash
# Set isolated test timeout (seconds)
pytest --isolated-timeout=60

# Disable subprocess isolation for debugging
pytest --no-isolation

# Combine with pytest debugger
pytest --no-isolation --pdb
```

### pytest.ini / pyproject.toml

```ini
[pytest]
isolated_timeout = 300
isolated_capture_passed = false
```

Or in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
isolated_timeout = "300"
isolated_capture_passed = false
```

## Use Cases

### Testing Global State

```python
@pytest.mark.isolated
def test_modifies_environ():
    import os
    os.environ["MY_VAR"] = "value"
    # Won't affect other tests

@pytest.mark.isolated
def test_clean_environ():
    import os
    assert "MY_VAR" not in os.environ
```

### Testing Singletons

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

### Testing Process Resources

```python
@pytest.mark.isolated
def test_signal_handlers():
    import signal
    signal.signal(signal.SIGTERM, custom_handler)
    # Won't interfere with pytest
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

**Performance**: Subprocess creation adds ~100-500ms per group. Group related tests to minimize overhead.

## Advanced

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

**Missing output**: Enable capture for passed tests with `isolated_capture_passed = true`

**Subprocess crashes**: Check for segfaults, OOM, or signal issues. Run with `-v` for details.

## Contributing

1. Install pre-commit: `pip install pre-commit && pre-commit install`
1. Run tests: `pytest tests/ -v`
1. Open an issue before submitting PRs for new features

## License

MIT License - see LICENSE file for details.
