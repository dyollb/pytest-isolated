# Isolation CLI Compatibility

This page separates options into behavior categories so it is clear what is
blocked, what is parent-handled, and what is forwarded.

Scope: isolation is active when using `@pytest.mark.isolated` or `--isolated`.

Implementation references:

- hard-block validation: `_validate_isolation_compatibility` in `src/pytest_isolated/config.py`
- block list source: `_INCOMPATIBLE_OPTIONS` in `src/pytest_isolated/config.py`
- forwarding behavior: `_build_forwarded_args` in `src/pytest_isolated/execution.py`

## Category A: Unsupported with isolation (hard error)

These options are rejected when isolation is active. pytest-isolated raises
`UsageError`.

### Interactive debugger options

- `--pdb`: requires interactive debugger access
- `--pdbcls`: requires interactive debugger access
- `--trace`: requires interactive debugger access
- `--full-trace`: tied to interactive exception display

### Import and discovery options

- `--confcutdir`: conftest search path differs between parent and child
- `--noconftest`: parent and child need conftest for consistency
- `--collect-in-virtualenv`: virtualenv detection may differ per process

### Cache and execution-state options

- `--nf`: file modification times differ between parent and child
- `--new-first`: file modification times differ between parent and child
- `--sw`: stepwise state tracking lost across subprocess boundary
- `--stepwise`: stepwise state tracking lost across subprocess boundary
- `--sw-skip`: stepwise state tracking lost across subprocess boundary
- `--sw-reset`: stepwise state tracking lost across subprocess boundary
- `--cache-show`: shows only parent cache, not subprocess cache
- `--cache-clear`: clears parent cache only, subprocess cache unaffected

### Configuration selection options

- `-c`: parent and child must use same config file
- `--config-file`: parent and child must use same config file

### Fixture and execution-control options

- `--setup-only`: setup-only prevents test execution in child
- `--setup-plan`: setup-plan prevents test execution in child

### Debug and diagnostics options

- `--trace-config`: traces parent conftest, not subprocess conftest
- `--debug`: subprocess debug output may be lost

### xfail behavior options

- `--runxfail`: xfail handling can be inconsistent between parent and child

## Category B: Supported, parent-handled, not forwarded to child

These options are supported for isolated runs, but are intentionally consumed in
the parent process and are not forwarded to child subprocesses.

- collection and selection options:
  `--co`, `--collect-only`, `--pyargs`, `--ignore`, `--ignore-glob`,
  `--deselect`, `--keep-duplicates`
- fixture listing options:
  `--fixtures`, `--funcargs`, `--fixtures-per-test`
- fixture tracing option:
  `--setup-show` (capture presentation is handled in parent)
- `--lf`, `--last-failed`
- `--ff`, `--failed-first`
- `-s`, `--capture`, `--capture=...` (capture mode is applied when building
  the subprocess command)
- internal plugin flags: `--isolated`, `--isolated-timeout`, `--no-isolation`
- positional test selectors/paths from CLI (children receive resolved nodeids)

## Category C: Forwarded to child by default

All other options are forwarded, including custom/third-party plugin options.

Examples include plugin loading and plugin-defined options such as `-p`,
`--disable-plugin-autoload`, and `--continue-on-collection-errors`.

This is the default blacklist model: if an option is not in Category A and not
explicitly parent-handled in Category B, it is forwarded.

## Bypass Mode

Use `--no-isolation` to run without subprocess isolation. In that mode, Category
A restrictions do not apply.

## Practical Workarounds

- debugger workflows: use `--no-isolation --pdb`
- fixture inspection workflows (`--fixtures`, `--setup-show`, etc.): run without isolation
- stepwise/new-first/cache-debug workflows: run without isolation
- plugin loading experiments (`-p`, `--disable-plugin-autoload`): run without isolation
