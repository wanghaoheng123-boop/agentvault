# AGENTS.md — Operational Law

Binding protocol for every coding agent in this repository. `CLAUDE.md` mirrors
this file, and `GEMINI.md`, `AGENTS.codex.md`, `.cursorrules`, `.windsurfrules`,
`.cursor/rules/agentvault.mdc` and `.github/copilot-instructions.md` all point
here — so this is law for Claude, Codex, Gemini, Cursor, Copilot and Windsurf
alike.

Cognitive hub: **`.agentvault/`** — start at `.agentvault/INDEX.md`.

> **Adopters:** record your project's test command in `.agentvault/INDEX.md`
> and adapt the test/lint steps in `.config/wt.toml` before relying on the
> `pre-merge` hook. Everything else works as shipped.

---

## 1. Workspace Entry Protocol

Run this on startup, before touching code. It prevents the two worst failure
modes: redoing finished work, and regressing it.

1. **Read** `.agentvault/INDEX.md`.
2. **Identify your branch:** `git rev-parse --abbrev-ref HEAD`.
3. **Check for an inherited handoff:**
   ```bash
   cat ".agentvault/handoffs/$(git rev-parse --abbrev-ref HEAD | tr / -).md" 2>/dev/null
   ```
4. **Check the board** for `UNASSIGNED`, `BLOCKED`, `IN_PROGRESS` or
   `NEEDS_CONTINUATION` work: `.agentvault/bin/av-board.py get`.
   An `IN_PROGRESS` task assigned to someone else may mean a live agent in
   another worktree. Do not take it over without evidence it was abandoned.
5. **Read** `MemoryBank/CURRENT.md`, run `bin/avcoord refresh --views-only`,
   then read `MemoryBank/board.md` or `bin/avcoord status`. If CURRENT is older
   than `stale_after_hours`, report **STALE**.

**If you are continuing someone else's work — in this order:**

1. Read the handoff manifest end to end, including *Already tried and rejected*.
2. **Run the reproducer command first.** Do not read code first and do not
   start fixing — you are confirming the failure state still exists.
   - Reproduces as described → resume from the documented blocker.
   - Passes unexpectedly → state changed. Do not assume done: run the full
     suite, then update the handoff with what you found.
   - Fails differently → you have a second problem. Record it before proceeding.
3. Trust the *Completed Sub-actions* list — do not redo it.
4. Claim it: `.agentvault/bin/av-board.py claim --id <id> --agent <you>`.

## 2. Strict Test-Driven Development

**Red — write the failing test first.** Before altering application code, write
a test that fails for the right reason, and *watch it fail*. A test that has
never failed proves nothing. Cover boundary cases (empty, single, max,
off-by-one, absent), the concurrency case where shared state is involved, and
the exception path — asserting the specific exception and message.

**Green — the minimal correct change.** Implement the least code that passes.
Do not opportunistically refactor in the same step; a green suite after a mixed
change does not tell you which part was right.

**Then re-run the whole suite.** A green unit with a red suite is a regression.

Bug fixes are the same loop: the reproducer *is* the Red test. Never fix a bug
you have not first reproduced in a test.

## 3. Graceful Handoff & Interruption Protocol (MANDATORY)

**Trigger:** before terminating a session, when context runs low, when blocked,
or whenever yielding control. Do this *while you still have enough context to
describe your own reasoning*.

**Step 1 — checkpoint commit**
```bash
git add -A
git commit -m "wip(<task-id>): checkpoint - <what works / what is broken>"
```
Commit broken code deliberately: the branch is isolated, and an uncommitted
tree is invisible to the next agent. If the hook rejects the commit for a
missing lease, see `.agentvault/memory/lessons-learned.md` L1 — the usual cause
is an unset `AVCOORD_RUN_ID`. **Never** use `--no-verify`.

**Step 2 — write the handoff.** Copy `.agentvault/handoffs/TEMPLATE.md` to
`.agentvault/handoffs/<branch>.md` and fill **every** field. Slashes in a branch
name flatten to dashes: `feat/payment` becomes `handoffs/feat-payment.md`, never
a nested directory. The two fields that carry the value are the **Reproducer
Command** (copy-pasteable, currently failing, with the exact failure text) and
**Blockers / Critical Context** (the reasoning not recoverable from the diff,
including dead ends). Handoffs are committed: no tokens, no secrets, no
absolute home paths.

**Step 3 — update the board**
```bash
.agentvault/bin/av-board.py pause --id <task-id>   # -> NEEDS_CONTINUATION
```

**Step 4 — release leases** you no longer need, and record in the handoff any
you deliberately kept.

**A session that ends without steps 1–3 is a protocol violation**, even if the
code is fine. The next agent inherits the code either way; it inherits the
*reasoning* only through the manifest.

## 4. Self-Healing Loop

Diagnose and repair runtime and test failures autonomously. Do not stop to ask
about an error you can read.

1. **Read the actual trace** — bottom frame, exact exception type and message.
2. **Reproduce in isolation** — one failing test, not the whole suite.
3. **One hypothesis, one change, re-run.** Changing several things at once
   means a green result teaches you nothing.
4. **Three failed hypotheses = stop guessing.** Add instrumentation, or re-read
   the failing code from the top.
5. **Bound the loop.** After ~5 failed cycles on the same error, hand off (§3)
   with everything you ruled out. A blocked handoff with good evidence beats a
   session burned on one traceback.

**Escalate instead of self-healing when:** the fix needs a destructive or
outward-facing action (`.agentvault/invariants/security.md` S5); the failure
implies a real invariant breach; or the "fix" would be deleting or weakening a
test or an invariant.

## 5. Invariants — read before changing shared state

- `.agentvault/invariants/security.md` — secrets, lease and path authorization,
  untrusted input, what needs a human.
- `.agentvault/invariants/concurrency-and-data.md` — leases, atomic writes,
  append-only journals, CAS, projections.

Two that catch people immediately:
- **Never create `.agentvault/MemoryBank/`** — the health check rejects the
  nesting as `DUAL_SSOT`.
- **Never hand-edit a projection.** Correct the journal, then re-project.

## 6. Worktree commands

Install Worktrunk and its shell integration before using `wt`. These hooks
use the 0.76 configuration schema; validate your installed version. Project hooks live at top-level keys in
`.config/wt.toml`; the default worktree path is a sibling of the main checkout.

```bash
# Start a task; pre-start registers it on the shared board under a lease.
wt switch -c codex/my-task -x claude -- "<task prompt>"

# Without Worktrunk:
git worktree add -b codex/my-task ../workspace.codex-my-task main
cd ../workspace.codex-my-task
python3 -B .agentvault/bin/av-lifecycle.py start

# Locate LIVE board, handoffs, invariants and lessons from any worktree:
HUB_PATH=$(python3 -B .agentvault/bin/av-board.py hub)
cat "$HUB_PATH/INDEX.md"
```

Do not replace a tracked `.agentvault/` directory with a symlink. The board CLI
resolves the canonical hub via Git's common directory; tracked code stays
isolated. Write handoffs and shared notes under `$HUB_PATH`, then checkpoint
those changes in the main checkout. Read ADR-0002 before changing this routing.
The avcoord lease CLI still operates on its invocation's repository root; use
`bin/avcoord` in the canonical main checkout for global coordination leases.

Resume an existing task with `wt switch <branch>` (or enter its existing path),
read the canonical handoff, run its reproducer, and then claim it. The start
hook refuses to take over a task actively assigned to another agent.

Before merge, commit the verified code and fill the canonical handoff. Run:

```bash
wt merge main --no-squash --no-rebase --no-remove
```

The pre-merge hook runs tests, compileall and an independent invariant audit.
Only post-merge closes the task: it verifies source-commit ancestry in target
HEAD, archives the handoff and records merge provenance under a shared lock
and lease. Review and checkpoint any resulting canonical hub changes in the
main checkout. The provenance entry is not a substitute for writing useful
lessons. Do not report a task MERGED merely because pre-merge passed.

---

## 7. Coordination engine: `avcoord` / MemoryBank

`.agentvault/` owns task routing and handoff; `bin/avcoord` is the coordination
engine underneath. Full protocol: `MemoryBank/coord/PROTOCOL.md`, quick
reference `MemoryBank/coord/PROTOCOL.quick.md`, gates
`MemoryBank/coord/EXECUTION_GATES.md`.

- **`bin/avcoord gate` is the deterministic DONE check.** Chat claims are not
  evidence; `verify` records evidence review and is not a test runner.
- The gate runs the health check **first and short-circuits**. If `doctor` is
  red, the gate never reaches your tests — run them directly for test health.
- Register a run before journal writes, then claim the exact path before
  writing it. `bin/avcoord -h` for the full CLI.
