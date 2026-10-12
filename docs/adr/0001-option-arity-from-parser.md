# 1. Option value-arity comes from pytest's parsed parser

Date: 2026-10-12
Status: Accepted

## Context

Isolated tests run in a child `pytest` process. Telling the child about the parent's configuration has been re-litigated repeatedly: forward-only-specific-args (#7), capture carve-out (#28), adding `--timeout` to the allowlist (#32), special-casing `--pdb` with a second gate (#36), then inverting to forward-everything with a blacklist (#49). Every round kept the same shape: hand-maintained sets of option names — including a hand-maintained list of *which options consume a value token* — plus a hand-rolled argv scanner.

That shape has a structural defect. The scanner must know each option's arity to decide whether the next token belongs to it, but only pytest's parser truly knows it (core options, plugin options, conftest-registered options). Two consequences, both verified on pytest 9:

- A third-party option in separate-token form (`--slowest 5`) was forwarded as bare `--slowest`; the child received the option without its value.
- Every new plugin option required a code change (#32 was exactly that), so isolation was always one plugin behind.

## Decision

The parent→child argument interface (`child_args(config)`) derives option arity from the parent's parsed pytest option table (`config._parser.optparser._option_string_actions`, an argparse `action.nargs` lookup) instead of a hand-maintained list. We accept that `_parser.optparser._option_string_actions` is a pytest-internal attribute as the pinned integration surface for this one lookup, guarded by a clear RuntimeError if it ever disappears.

Policy stays ours: one table maps option names to `blocked` / `parent-handled` (everything else forwards). The table contains names and reasons only — never arity. `--rootdir` is stated exactly once (the parent's resolved rootpath replaces any user form). Validation of blocked options reads *parsed state* (`config.option`), so CLI, `PYTEST_ADDOPTS`, and `addopts` ini are enforced identically, and the child keeps receiving addopts via environment inheritance.

## Consequences

- The "dropped/misaligned value" bug class is structurally impossible, and options from plugins released after pytest-isolated forward correctly with zero code changes.
- Adding or reclassifying a compatibility fact is a single-row edit in the policy table; the docs' Category A/B/C lists are test-guarded against the table.
- The scanner, the two `*_WITH_VALUE` sets, and the separate `--pdb` gate are deleted; blocking has one enforcement point.
- Risk: a pytest version that reorganizes `Config._parser` breaks the lookup loudly (RuntimeError with version context), not silently. Until proven otherwise on newer pytest, this is the one place reaching into pytest internals.

## Rejected alternatives

- **Serialize the parsed namespace to the child** (skip argv entirely): deletes the whole token problem but makes child startup a protocol we own and re-enters plugin-arg reconstruction risk. Re-evaluate only if argv forwarding shows further friction.
- **Keep hand-maintained arity lists**: status quo ante; demonstrably one plugin release behind, and it silently mangles commands it doesn't know.
