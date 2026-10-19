# pytest-isolated

A pytest plugin that runs marked tests in fresh subprocesses so crashes, state
pollution, and hangs stay contained. Terms below are specific to this context;
pytest's own vocabulary (marker, hook, nodeid, Item) is assumed.

## Language

### Roles

**Parent**:
The pytest process the user invokes; it selects, plans, runs, and reports,
while children do the executing.
_Avoid_: main process, host process

**Child**:
A fresh pytest process that runs one Isolated Group and reports its results
back to the Parent.
_Avoid_: subprocess (a mechanism word, not the domain noun), spawned process

**Isolation**:
Running an Isolated Group in a fresh Child instead of in the Parent's process.
Active when a run has isolated-marked tests or `--isolated`.
_Avoid_: sandboxing, forking (Children are spawned, not forked)

**Isolated Group**:
The set of tests that share one Child. An explicit group name wins; otherwise
the closest marker scope decides — a function's own marker isolates it alone, a
class marker groups its methods, a module marker groups the module.

**Group timeout**:
The maximum run time one Group's Child gets (marker > CLI/ini default). A
Parent-side run parameter on the Group, deliberately not part of what the Child
is told.

**Parent Session Fixtures**:
The session-scoped autouse fixture stack the Parent sets up itself before the
first Child launches and tears down at loop end, so session setup works even
when every Item runs in a Child. Children keep their own fixture stack; no
fixture value crosses the process boundary.
_Avoid_: shared fixtures (nothing is shared), global setup

### The child's interface

**Launch plan**:
The complete description of what one Child is told: its command line (with the
root directory stated exactly once, and — when the Group runs in a Group Pool
— a private basetemp and cache dir named after the Group's slot in the Pool),
environment, working directory (derived exactly once, a separate fact from the
stated one — the two can never disagree), and Report channel. Fresh plan per
Group.
_Avoid_: launch command, spawn setup

**Report channel**:
The file a Child appends its per-test result records to, which the Parent
reads to classify and re-emit the Group's outcome.
_Avoid_: report file, results file

**Child marker**:
The environment fact telling a pytest process it is a Child: it stops the
Child from isolating again and points it at its Report channel.
_Avoid_: recursion guard (it is what makes a Child a Child, not just a guard)

**Partial-output invariant**:
Every Child runs with unbuffered output duplicated to the Parent's view, so a
timeout or crash still yields everything printed so far. The one carve-out: a
Parent run with capture disabled (`-s`) gives the Child the shared terminal
directly — output is live and interleaved, and nothing is attached afterward.
_Avoid_: tee-sys hack

### Parallel running

**Group Pool**:
The Parent's set of concurrently running Children, capped by the process count
(`--isolated-numprocesses`); pending Groups queue in collection order as slots
free. Each Group's captured output reaches the Parent's terminal as one block
when its Child finishes (completion order); reported results keep collection
order.
_Avoid_: worker pool (there are no workers — each Child runs one whole Group),
burst-on-completion (a property of the Pool, not its own thing)

**Sweep**:
One round of the Group Pool's running cycle: Snapshot each live Child's plain
facts (liveness from one poll, its deadline, when a terminate was requested),
Decide what each Child gets (terminate-for-timeout, escalate-to-kill, reap),
Apply the decisions through the Child's mechanics, and Reap finished Children
in running order. The timeout contract lives in the Decide step, not the loop.
_Avoid_: Sweep Tick, tick (jargon; the round is a sweep, as in `decide_sweeps`),
polling loop, housekeeping (the four stages are the term, not a
mechanism description)

**Exclusive Group**:
A Group marked `serial=True`, which runs alone: admitted only while no other
Child runs, and nothing launches while it does. It keeps its queue position, so
it can never be starved.

**Interrupted run**:
The Parent's outcome when Ctrl-C stops a run: Children are killed and every test
that never reached teardown is reported as a failure.

### Option policy

**Blocked option** (Category A):
An option incompatible with isolation; the run is rejected with an explanation
before any Child starts.

**Parent-handled option** (Category B):
An option the Parent fully consumes and resolves before the split; the Child
receives the resolved outcome (Nodeids, the stated root directory), never the
option itself.

**Forwarded option** (Category C):
The default: anything neither Blocked nor Parent-handled is re-stated to the
Child with its values intact.

**Forwarding**:
Re-stating the Parent's options to the Child in command-line token form, each
option's Arity taken from pytest's own parsed option table (ADR-0001).

**Arity**:
How many value tokens an option consumes. Only pytest's parsed parser truly
knows it; it is derived, never hand-maintained.

**Enforcement point**:
The single gate where Blocked options are detected from parsed state,
whichever route they arrived by (command line, environment, ini).
_Avoid_: gate (plural gates are the pre-#49 shape this replaced)

**Selector**:
A positional test path the user typed. The Parent resolves Selectors to
nodeids; a Child only ever receives nodeids, never Selectors.
_Avoid_: positional argument (after `--`, even option-looking tokens are
Selectors)

### Modes

**Bypass mode**:
Running with isolation switched off for debugging (`--no-isolation`); Blocked
options become legal because no Child is ever launched.
