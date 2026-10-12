# Spec: Collapse child launch into one deep launcher — `plan_launch(config, nodeids) -> LaunchSpec`

> Target: `dyollb/pytest-isolated` · Label: `ready-for-agent`
> Origin: architecture review 2026-10-11, candidate "Collapse child launch into one deep launcher" (card 2). Consumes card 1 (`child_args`, draft PR #59). Supersedes card-1 spec decision "Child command assembly itself stays in the run loop for now".

## Problem Statement

As a user of `pytest-isolated`, everything the isolated child subprocess is told — its command line, its environment, its working directory, and the file handle it must write results to — is assembled inline inside the plugin's run loop, one long interleaved block. The observable consequences: a child that runs without unbuffered output, or without tee-sys capture, loses the partial output that makes timeout/crash reports useful; the working directory the child runs in must match how the parent resolved rootpath or the nodeids the parent selected stop resolving; each new child-facing fact in the plugin's history (the capture carve-out #28, rootdir handling) required editing the run loop itself, and the rootdir fact was at one point stated twice on the child command line with last-one-wins semantics.

As a maintainer, `pytest_runtestloop` is ~150 lines mixing four responsibilities: planning the launch (temp report file, env copy, capture policy, argv assembly, cwd), running the subprocess, classifying its outcome through four crash/timeout handlers, and emitting results. Any test of "what does the child see?" requires spawning a real child, because the plan exists only as locals inside the loop — there is no interface to call. Capture ping-pong (#28) and rootdir/cwd facts are loop concerns today, and every future child-facing flag means another in-loop edit at the same hot spot card 1 identified.

## Solution

Extract the plan portion of the loop into one deep module, `plan_launch`, returning a `LaunchSpec` — a value carrying the child's entire interface: `cmd`, `env`, `cwd`, `report_path`. `plan_launch` calls card 1's `child_args(config)` internally, so the run loop never touches option forwarding, capture policy, env construction, or temp-file creation again. The loop shrinks to orchestration: build nodeids, resolve the group timeout, `plan_launch → run → parse → classify (four handlers) → emit → maxfail`. The subprocess adapter (`_run_subprocess`) stays exactly as it is below the seam — one real adapter is fine; the seam pays off because the plan is now assertable without spawning anything.

This is a behavior-preserving refactor: the child's command line, environment, cwd, and report channel are byte-identical to today's. Capture policy (tee-sys default, `-s` when the user asked for no capture — the #28 decision) is relocated, not revisited, and becomes a launcher invariant.

## User Stories

1. As a pytest user, I want the isolated child launched with unbuffered output and tee-sys capture, so that partial output still appears in timeout and crash reports just as it does today.
2. As a pytest user who ran with `-s` or `--capture=no`, I want the child to run with capture disabled too, so that the #28 carve-out survives the refactor unchanged.
3. As a pytest user, I want the child's own `--capture` setting never forwarded, so that only the parent's terminal plugin controls what I see (parent-handled, Category B).
4. As a pytest user, I want the child to run in the parent's resolved root directory (rootpath, falling back to the invocation directory), so that the nodeids the parent selected always resolve inside the child.
5. As a pytest user, I want `--rootdir` stated exactly once on the child command line (from the forwarding module) and the working directory derived exactly once (from the launcher), so that the two rootdir-adjacent facts can never silently disagree.
6. As a pytest user, I want each group's run to get its own fresh JSONL report file passed to the child via the environment, so that results from one group never leak into another group's classification.
7. As a pytest user, I want the child environment to carry the plugin's subprocess marker and report-path variables (and otherwise be the parent's environment, including `PYTEST_ADDOPTS`), so that the child short-circuits isolation and writes its records, exactly as today.
8. As a pytest user, I want all of card 1's forwarding behavior (third-party option values intact, blocked options rejected, plugin-internal flags not forwarded) preserved through the new launcher, so that PR #59's fixes are unaffected by this refactor.
9. As a pytest user, I want `--no-isolation` to keep bypassing the entire child-launch path, so that interactive debugging remains supported.
10. As a maintainer, I want one edit point for any fact about the child's interface (argv, env, cwd, report channel), so that the next #28-style child-facing change is a change to `plan_launch`, not to the run loop.
11. As a maintainer, I want capture policy expressed once inside the launcher module, so that capture ping-pong has a single home with an explanatory comment about why tee-sys + `-u` exist.
12. As a maintainer, I want `pytest_runtestloop` reduced to orchestration (plan → run → parse → classify → emit → maxfail), so that reading the loop reads like the algorithm rather than like setup bookkeeping.
13. As a maintainer, I want the run loop to no longer call `child_args` directly, so that there is exactly one caller of the forwarding interface and the child-argv order (base flags, forwarded args, nodeids) is a launcher invariant.
14. As a maintainer, I want the child timeout to remain a parent-side run parameter rather than part of `LaunchSpec`, so that the spec stays purely "what the child sees" and group-timeout resolution stays with the loop that owns groups and markers.
15. As a maintainer, I want the subprocess adapter (`_run_subprocess`) untouched below the seam, so that crash detection, timeout handling, and partial-output preservation are not disturbed by the refactor.
16. As a test author, I want to assert the child's planned command line by calling `plan_launch` with a real parsed config and plain string nodeids, so that I test a pure function with no mocks, no fabricated Items, and no spawned processes.
17. As a test author, I want to assert the planned env contains the two plugin subprocess variables (and the parent's inherited environment) at the same unit seam, so that "child sees X" env claims are testable without spawning.
18. As a test author, I want to assert the planned cwd and `report_path` (fresh file per call, JSONL convention) at the unit seam, so that the launcher's four output facts are all directly inspectable.
19. As a test author, I want the existing pytester end-to-end suite (timeout partial output, crash classification, `-s` respect, `--setup-show` re-emit, custom-option forwarding) to pass unchanged, so that behavior preservation is proven by the tests that already exist rather than by new ones.
20. As a downstream packager, I want this refactor to require no version bump and no user-facing change at all, so that it can ride along with the card-1 release rather than competing with it for release notes.
21. As a future contributor, I want `LaunchSpec` to be a plain immutable value (NamedTuple or frozen dataclass), so that tests can destructure and compare it field-by-field without helper accessors.
22. As a future contributor, I want the report temp-file creation inside `plan_launch` (same naming convention as today), so that the plan is a complete description of the launch and the loop never handles file plumbing.

## Implementation Decisions

- **New module `launch.py`** in the plugin package, alongside `forwarding.py`. Its public interface is exactly two names: `LaunchSpec` and `plan_launch`. Everything else is private.
- **`LaunchSpec`** is an immutable value (frozen dataclass or NamedTuple, matching the repo's existing `SubprocessResult` NamedTuple style) with exactly four fields: `cmd: list[str]`, `env: dict[str, str]`, `cwd: str`, `report_path: str`.
- **`plan_launch(config, nodeids) -> LaunchSpec`** — takes the parent `pytest.Config` and the group's nodeid strings. Taking `list[str]` (not Items) keeps the seam free of fabricated test doubles and mirrors card 1's `child_args(config) -> list[str]` unit-seam style. Group-name and timeout resolution are loop-level orchestration and deliberately stay out.
- **Assembly order is preserved exactly**: `[sys.executable, "-u", "-m", "pytest"]`, then the capture flag (`-s` if the parent's parsed capture is `"no"`, else `--capture=tee-sys`), then `child_args(config)`'s output (which includes the single `--rootdir`), then the nodeids. `-u` and tee-sys stay together with a comment explaining the partial-output invariant.
- **`plan_launch` calls `child_args` internally**; `execution.py` drops its direct import of `child_args`. `forwarding.py` remains the sole owner of option policy; `launch.py` owns argv packaging plus env, cwd, and the report channel.
- **Env construction relocates unchanged**: `os.environ.copy()` plus the subprocess marker and report-path variables (constants stay in `config.py`, both modules import them). `PYTEST_ADDOPTS` continues to reach the child by inheritance only — no re-emission (card 1 decision).
- **Report file creation relocates unchanged**: fresh `NamedTemporaryFile(delete=False)` with today's prefix/suffix convention, per plan (i.e., per group as today).
- **cwd derivation relocates unchanged**: `str(config.rootpath)` when set, else the parent's invocation directory. Distinct from the `--rootdir` child argument; the launcher is the only place cwd is decided.
- **`_run_subprocess` is called with `(spec, timeout)`**, keyword-only (matching the repo's PLR0917 convention under newer ruff), and internally uses the spec's fields with today's `subprocess.run` call unchanged. Timing measurement and the four crash handlers, `--setup-show` stdout re-emission, `_parse_results`, `_emit_all_results`, and the maxfail check all stay in the loop — they consume results, which is orchestration.
- **Behavior-preservation constraint**: for any parent config and nodeid list, the spawned argv/env/cwd/report channel must be identical to today's. This refactor changes where facts are stated, never which facts are stated.
- **No version bump** (explicit user instruction); `__version__` stays as-is on the card-1 branch value.
- **No new ADR**: ADR-0001 (arity from parser) is unaffected — the child still receives a command line; this spec only packages it. The card-1 spec's "command assembly stays in the run loop" decision is superseded by publishing this spec.

## Testing Decisions

- **Two layers, no new mechanisms.** Primary layer: the existing pytester end-to-end suite passes **unmodified** — this is the behavior-preservation proof. It already covers every child-visible behavior the launcher owns: partial output on timeout (which exercises `-u` + tee-sys together, `tests/test_execution.py`), crash classification, `-s` handling, `--setup-show` re-emit, and the #48 custom-option-forwarding test (`tests/test_options.py`).
- **New unit seam**: `plan_launch(pytester.parseconfig(...), ["some.py::test_a"])` asserted field-by-field, in a new `tests/test_launch_plan.py` alongside card 1's `tests/test_child_args.py`. Cases: base-command prefix; `--capture=tee-sys` by default and `-s` under `-s`/`--capture=no`; nodeids appended last in order; forwarded args present and nodeids after them (via `_meaningful`-style filtering of parser noise as in `test_child_args.py`); `--rootdir` appears exactly once in `cmd`; cwd equals rootpath when set; env contains both plugin subprocess variables with correct values and inherits a sentinel variable set in the parent env; `report_path` ends in the JSONL suffix, the file exists after planning, and two calls produce distinct paths.
- **Only test external behavior**: assertions target `LaunchSpec` fields (the child's observable interface), never private helpers; no Mock objects anywhere — card 1 deleted the Mock-based tests precisely so this seam would be pure.
- **Prior art**: `tests/test_child_args.py` (real `parseconfig` + `_meaningful()` filtering; `TestRootdirStatedOnce` count-assertion pattern) for the unit layer; `tests/test_execution.py` timeout/partial-output tests for the e2e invariants.
- **Regression pinning**: as a pure refactor, the pin is the existing suite's unchanged pass; new tests additionally pin invariants (#28 capture choice, single rootdir, env contract) that were previously only implied by loop internals.

## Out of Scope

- **Seaming the subprocess adapter** behind a runner interface — the review's card 2 explicitly accepts one real adapter; crash/timeout handlers are already unit-tested against the result value today.
- **Parsed-state transfer** (card 4, speculative) — the child still receives a command line to interpret.
- **Moving timeout into `LaunchSpec`** or restructuring group-timeout resolution.
- **Report temp-file lifecycle cleanup** (today the temp files are never unlinked) — noted as a follow-up, not a launcher-seam concern.
- **Re-deciding the capture policy** from #28 — relocated verbatim.
- **Parallel group execution**, and any change to grouping, markers, crash-handler logic, or reporting.
- **Version bump or changelog/release work** (explicitly excluded by the user).

## Further Notes

- Sequencing: this spec depends on card 1 (`child_args`, draft PR #59) being merged, or stacks on its branch — `plan_launch` wraps its output and the two specs share `tests/test_child_args.py` conventions.
- Report temp files currently leak (`delete=False`, no unlink anywhere). A `plan_launch` follow-up could pair creation with cleanup in the loop's `finally`, but that changes lifecycle behavior and is deliberately excluded here.
- `--setup-show` stays forwarded to the child (deliberate, per card 1: the child prints SETUP lines, the parent re-extracts them from stdout). The extraction stays in the loop because it is output consumption, not planning.
- CI gotcha carried from card 1: run the pinned pre-commit ruff **and** a latest-ruff check (PLR0917 keyword-only convention); CI installs tools unpinned.
- Domain language is the Category A/B/C vocabulary from `docs/incompatible-options.md` (no GLOSSARY.md in this repo).
