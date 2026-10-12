# Spec: Deepen arg forwarding — `child_args(config)` with parser-derived option arity

> Target: `dyollb/pytest-isolated` · Label: `ready-for-agent`
> Origin: architecture review 2026-10-11, candidate "Deepen arg forwarding" (card 1, with card 3's policy table folded in).

## Problem Statement

As a user of `pytest-isolated`, I run my suite with plugin options the plugin author has never heard of — `--slowest 5`, `--randomly-seed=42`, `--cov-report=xml` — and the isolated child subprocess silently receives a mangled version of my command line:

- A third-party option in separate-token form (`--slowest 5`) is forwarded **without its value** (the child sees bare `--slowest` and either errors or guesses). Only options hand-listed in the plugin's "takes a value" sets keep their value.
- Options set via `PYTEST_ADDOPTS` are enforced for blocking by a separate hand-rolled path but are **never seen by the forwarder at all** — the child merely inherits the env var, so blocking and forwarding can disagree about what options are active.
- The same incompatibility fact (e.g. `--pdb`) is enforced in three places with three different mechanisms, so past fixes (#36) had to add redundant gates, and each new incompatible option historically meant touching multiple files and hand-maintained lists.

As a maintainer, the forwarding module is shallow: its interface — five hand-maintained sets of option strings plus docs duplicating them — is as complex as its implementation, and the token scanner re-derives facts (which options consume a value, in which token forms) that pytest's own parser already knows. The design has flip-flopped (allowlist in #7, blacklist inversion in #49, carve-outs in #28/#32/#36) without the shape ever improving.

## Solution

One deep module at the parent→child seam: a single function `child_args(config)` that returns the complete list of arguments the child pytest should receive. Contributors and callers no longer know any option-name sets or token-scanning rules:

- **Value arity comes from pytest's parser**, not a hand-maintained list. The module asks the parent's parsed parser which options consume values (and how many), so `--opt value`, `--opt=value`, option clusters, and third-party plugin options are all handled correctly with zero per-option code.
- **One policy table** of option name → kind (`blocked` / `parent-handled` / `forwarded`) with a reason string replaces the five scattered sets. Blocking validation and forwarding consult the same rows.
- **`PYTEST_ADDOPTS` is parsed once in the parent** and flows through the same table, so enforcement and forwarding see identical inputs.
- The token scanner and the shlex workaround are deleted; the interface is one function returning one `list[str]`.

## User Stories

1. As a pytest user with third-party plugins, I want my plugin's options (e.g. `--slowest 5`) forwarded to isolated subprocesses with their values intact, so that plugin behavior applies inside isolated tests just as it does in normal runs.
1. As a pytest user, I want options in `--opt=value` form forwarded unchanged, so that my preferred invocation style works with isolation.
1. As a pytest user, I want short flag clusters (e.g. `-qxl`) forwarded, so that compact invocations still isolate correctly.
1. As a pytest user, I want options whose values start with `-` (e.g. `-k "-not_this"`) forwarded as option+value, so that negated expressions aren't dropped from the child run.
1. As a pytest user, I want `PYTEST_ADDOPTS` options to reach the child exactly as CLI options do, so that my CI-configured defaults apply to isolated tests.
1. As a pytest user, I want a blocked option specified via `PYTEST_ADDOPTS` rejected with the same error as on the CLI, so that there is no way to sneak an unsupported option past validation.
1. As a pytest user, I want a clear `UsageError` naming the blocked option, its reason, and the escape hatch (`--no-isolation`), so that I can fix my invocation in one step.
1. As a pytest user, I want plugin-internal flags (`--isolated`, `--isolated-timeout`, `--no-isolation`) consumed by the parent and never forwarded, so that the child doesn't recursively re-enable isolation.
1. As a pytest user, I want parent-resolved selection options (`--ignore`, `--deselect`, `--lf`, …) NOT forwarded, so that the child runs exactly the nodeids the parent selected, without double-filtering.
1. As a pytest user, I want positional test paths never forwarded, so that the child receives only the parent-resolved nodeids.
1. As a pytest user with an exotic plugin option the maintainer never anticipated, I want my option forwarded by default (blacklist model), so that isolation works with plugins released after pytest-isolated.
1. As a plugin author, I want a custom option I registered in my conftest (`--eval-solution-path`) forwarded in both `--opt value` and `--opt=value` forms, so that issue #48's fix never regresses for any token form.
1. As a maintainer, I want one policy table (name → kind → reason) instead of five scattered sets, so that adding or reclassifying an option is a single-row edit.
1. As a maintainer, I want option-value arity derived from pytest's parser, so that the class of "value dropped/misaligned" bugs is structurally impossible.
1. As a maintainer, I want blocking validation and forwarding to consult the same table rows, so that #36-style redundant gates can never be needed again.
1. As a maintainer, I want the compatibility docs generated from the policy table, so that docs and code can't drift.
1. As a maintainer, I want the parent→child argument decision expressed by one function, so that any future design flip (allowlist ↔ blacklist) is a table change, not a scanner rewrite.
1. As a maintainer, I want `--rootdir` stated exactly once on the child command line, so that forwarding a user-supplied rootdir and injecting the parent's rootdir can't silently disagree.
1. As a test author, I want to assert the child's argv by calling the module's interface with a real parsed config, so that tests don't mock internals or spawn subprocesses.
1. As a test author, I want end-to-end pytester tests proving the child *received* an option (via a fixture reading it inside an isolated test), so that tests verify observable behavior, not argv strings produced by internals.
1. As a downstream packager, I want unknown/`=`-joined/token forms handled consistently on every pytest version the package supports, so that no pytest upgrade reintroduces argv-mangling regressions.
1. As a CI engineer, I want blocked-option errors to fire before collection starts, so that misconfigured isolation jobs fail fast with the actionable message.
1. As a debugging user, I want `--no-isolation` to bypass all of this, so that I always retain a supported path for interactive debugging.

## Implementation Decisions

- **New deep module** owning the parent→child argument seam. Its interface is one function, `child_args(config) -> list[str]`. Everything else — the policy table, parser interrogation, blocked-option validation — is private implementation. The parser-arity lookup is recorded as ADR-0001.
- **Single policy table**: each entry maps an option name to a kind (`blocked`, `parent-handled`, `forwarded`) plus a user-facing reason for blocked kinds. This absorbs today's five sets: `_INCOMPATIBLE_OPTIONS`, `_INCOMPATIBLE_OPTIONS_WITH_VALUE`, `_PARENT_HANDLED_FLAGS`, `_PARENT_HANDLED_WITH_VALUE`, `_FORWARDED_OPTIONS_WITH_VALUE`. The two "with value" sets are **deleted entirely** — arity is no longer declared by us (decision from the grilling of the architecture review; verified against pytest 9: the parent's parser exposes per-option `nargs` for every registered option, including conftest/plugin-registered ones).
- **Parser-derived arity**: at call time the module consults the parent config's parsed parser to learn which option strings consume values and how many. This makes third-party options, `-o`/`-p`-style repeatables, and cluster handling correct by construction. The hand-written token scanner is deleted.
- **Unknown options are forwarded verbatim with arity caution**: an option unknown even to the parser is forwarded as-is and never consumes a following token (preserves today's deliberate "don't swallow positional selectors" behavior). In practice pytest rejects unregistered options at parse time, so this branch is defensive; selector safety is pinned by unit tests at the interface.
- **Addopts unified by parsed state, not re-emission**: pytest itself merges `PYTEST_ADDOPTS` (and the `addopts` ini) into the parsed option state, and the child inherits the environment variable. Blocked-in-addopts is therefore caught by the same parsed-state check as blocked-on-CLI — one enforcement point, no shlex re-parse — and forwarded-in-addopts reach the child unchanged via inheritance. This refines the original draft (re-emit tokens + child-side suppression sentinel): implementation proved parsed-state validation plus inheritance covers both directions with less machinery.
- **Blocking validation moves behind the table**: the existing incompatible-options validator and the separate `--pdb` gate (which re-checks the parsed option after collection) both become readers of the same table; the pdb special case collapses into a normal `blocked` row evaluated against parsed state, restoring "one fact, one enforcement point".
- **Capture mode stays parent-handled** exactly as today (child uses `tee-sys`, or `-s` when the user asked for no capture). This spec does not re-decide #28; the decision merely relocates into the table + launcher handoff.
- **`--rootdir` duplication fixed** as part of adopting the module: rootdir injection becomes part of the child-args decision (stated once, parent rootpath winning), not a later append in the run loop.
- **Child command assembly itself (env, cwd, capture, temp report path) stays in the run loop for now** — that's the sibling candidate "Collapse child launch into one deep launcher" and is explicitly downstream of this spec: the launcher will consume `child_args`, but this spec ships value without it.
- **Compatibility surface**: no change to any user-facing flag, marker, or error type. `UsageError` messages keep their current shape (option name + reason + `--no-isolation` hint).
- **Docs**: the compatibility doc's Category A/B lists are machine-checked against the policy table by tests, ending the fourth hand-copy of the same facts.

## Testing Decisions

- **Only test external behavior**: (a) what the child *receives*, observed end-to-end; (b) what `child_args` *returns* for a given parsed config. Never test private helpers or token-walking mechanics.
- **Primary seam — pytester end-to-end** (existing, highest): tests invoke the plugin the way users do and assert child behavior through fixtures that read the forwarded option inside an isolated test. Prior art: the #48 regression test (`test_custom_option_forwarded_to_subprocess`) and the addopts inheritance test (`test_pytest_addopts_env_var_forwarded`). New/extended cases: third-party option with separate-token value; `=`-form of the same; blocked option via `PYTEST_ADDOPTS` producing the same UsageError; value starting with `-`; plugin-internal flags absent in child.
- **Unit seam — `child_args` against a real `pytest.Config`** (existing file location, re-shaped): build configs through pytest's own config-parsing (pytester/`parseconfig`), call the interface, assert the returned argv exactly. This is the single seam replacing today's Mock-based scanner tests.
- **Delete the Mock-based `_build_forwarded_args` unit tests** as the old interface's tests are retired: they assert token-scanning mechanics (and poke parser attributes onto mocks), which is testing past the interface; their *intent* (e.g. "unknown flags don't swallow positional selectors") survives re-expressed at the `child_args` seam.
- **Blocking tests stay UsageError-level** (message contains option name and reason), extending the existing options-test family; one new case proves CLI and addopts paths raise identically.
- **Docs consistency check**: a test (or generated-docs build step) asserting the published compatibility categories match the policy table.
- **Regression pinning**: every defect this spec fixes gets a failing-first test on current `main` (the dropped-value probe is already reproduced: `["--slowest", "5", "-v", "t.py::test_a"]` forwards `["--slowest", "-v"]`).

## Out of Scope

- The child **launcher** deepening (`plan_launch`/`LaunchSpec`: env, cwd, report path, capture policy, run-classify-emit loop refactor) — sibling candidate, consumes this module's output later.
- **Parsed-state transfer** (serializing the parent's parsed namespace instead of argv) — the review's card 4, explicitly speculative.
- Any change to **grouping semantics**, marker precedence, timeouts, or crash/xfail handling.
- Changes to **which options are blocked/parent-handled** (policy content unchanged; only its representation and enforcement move).
- Reworking the **capture-mode decision** from #28 (it is relocated, not revisited).
- pytest version-support changes; the parser-metadata interface is treated as the pinned integration surface.

## Further Notes

- Verified against pytest 9.0.2 on this repo's HEAD: value-arity is exposed per option string via the parent's parsed parser (including conftest-registered custom options), and `invocation_params.args` excludes `PYTEST_ADDOPTS` tokens — confirming the enforcement/forwarding asymmetry motivating the addopts decision.
- History this spec closes out: #7 (allowlist) → #28 (capture carve-out) → #32 (add `--timeout`) → #36 (pdb gates) → #49 (blacklist inversion). Each round re-touched the hand-maintained sets; deriving arity from the parser is what makes the next flip cheap and the bug class impossible.
- Domain language is the Category A/B/C vocabulary from the project's own compatibility doc (no GLOSSARY.md). The "arity comes from the parser" decision is recorded as the repo's first ADR: `docs/adr/0001-option-arity-from-parser.md`.
