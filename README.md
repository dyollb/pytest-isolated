# pytest-isolated

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

@pytest.mark.subprocess
def test_isolated():
    # Runs in a fresh subprocess
    assert True
```

Tests with the same group run together in one subprocess:

```python
@pytest.mark.subprocess(group="mygroup")
def test_one():
    shared_state.append(1)

@pytest.mark.subprocess(group="mygroup")
def test_two():
    # Sees state from test_one
    assert len(shared_state) == 2
```

Tests without an explicit group are automatically grouped by module.

## Configuration

### Command Line

```bash
# Set subprocess timeout (seconds)
pytest --subprocess-timeout=60

# Disable subprocess isolation for debugging
pytest --no-subprocess

# Combine with pytest debugger
pytest --no-subprocess --pdb
```

### pytest.ini / pyproject.toml

```ini
[pytest]
subprocess_timeout = 300
subprocess_capture_passed = false
```

Or in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
subprocess_timeout = "300"
subprocess_capture_passed = false
```

## Use Cases

### Testing Global State

```python
@pytest.mark.subprocess
def test_modifies_environ():
    import os
    os.environ["MY_VAR"] = "value"
    # Won't affect other tests

@pytest.mark.subprocess
def test_clean_environ():
    import os
    assert "MY_VAR" not in os.environ
```

### Testing Singletons

```python
@pytest.mark.subprocess(group="singleton_tests")
def test_singleton_init():
    from myapp import DatabaseConnection
    db = DatabaseConnection.get_instance()
    assert db is not None

@pytest.mark.subprocess(group="singleton_tests")
def test_singleton_reuse():
    db = DatabaseConnection.get_instance()
    # Same instance as previous test in group
```

### Testing Process Resources

```python
@pytest.mark.subprocess
def test_signal_handlers():
    import signal
    signal.signal(signal.SIGTERM, custom_handler)
    # Won't interfere with pytest
```

## Output and Reporting

Failed tests automatically capture and display stdout/stderr:

```python
@pytest.mark.subprocess
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

**Debugging**: Use `--no-subprocess` to run all tests in the main process for easier debugging with `pdb` or IDE debuggers.

**Performance**: Subprocess creation adds ~100-500ms per group. Group related tests to minimize overhead.

## Advanced

### Timeout Handling

```bash
pytest --subprocess-timeout=30
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

**Tests timing out**: Increase timeout with `--subprocess-timeout=600`

**Missing output**: Enable capture for passed tests with `subprocess_capture_passed = true`

**Subprocess crashes**: Check for segfaults, OOM, or signal issues. Run with `-v` for details.

## License

MIT License - see LICENSE file for details.

## Changelog

### 0.1.0 (2026-01-12)

- Initial release
- Process isolation with subprocess marker
- Smart grouping by module or explicit group names
- Timeout support
- Complete test phase capture (setup/call/teardown)
- JUnit XML and standard reporter integration
- Comprehensive error handling and reporting
