# pytest-isolated for pytest-forked Users

Quick reference for users familiar with pytest-forked.

## Basic Marker

| pytest-forked         | pytest-isolated         |
| --------------------- | ----------------------- |
| `@pytest.mark.forked` | `@pytest.mark.isolated` |

```python
# pytest-forked
@pytest.mark.forked
def test_crash():
    pass

# pytest-isolated
@pytest.mark.isolated
def test_crash():
    pass
```

## Run All Tests in Subprocesses

| pytest-forked     | pytest-isolated     |
| ----------------- | ------------------- |
| `pytest --forked` | `pytest --isolated` |

## Class and Module Markers

Both plugins support class and module markers:

```python
# Works in both plugins
@pytest.mark.forked  # or @pytest.mark.isolated
class TestDatabase:
    def test_setup(self): ...
    def test_query(self): ...

# Works in both plugins
import pytest
pytestmark = pytest.mark.forked  # or pytest.mark.isolated
```

## Grouping Tests

| pytest-forked | pytest-isolated                                                          |
| ------------- | ------------------------------------------------------------------------ |
| Not available | `@pytest.mark.isolated(group="name")` or `@pytest.mark.isolated("name")` |

In pytest-isolated, tests with the same group share a subprocess, reducing overhead.

## Platform Support

| pytest-forked                         | pytest-isolated                           |
| ------------------------------------- | ----------------------------------------- |
| Linux, macOS only (requires `fork()`) | Linux, macOS, Windows (uses `subprocess`) |

## Debugging

| pytest-forked      | pytest-isolated         |
| ------------------ | ----------------------- |
| No built-in option | `pytest --no-isolation` |

## Output Capture

Both plugins capture stdout/stderr for failed tests.
