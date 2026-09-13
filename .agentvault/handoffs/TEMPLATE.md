<!--
  AgentVault handoff manifest.
  Copy to .agentvault/handoffs/<branch>.md and fill EVERY field.
  Written for a cold agent with zero conversation history. If a field is
  genuinely N/A, write "N/A — <why>". Never leave a placeholder in place.
  Handoffs are committed and pushed: no tokens, no secrets, no absolute home
  paths. See .agentvault/invariants/security.md S1, S4.
-->

# Handoff: <branch-name>

## Task ID / Goal
- **Task ID:** `<id from .agentvault/tasks/board.json>`
- **Goal:** <one sentence: what "done" means, in terms an outsider can check>
- **Board status at time of writing:** `NEEDS_CONTINUATION`

## Authoring Agent / Session ID
- **Agent:** `<agent name, e.g. Agent-Alpha>`
- **Session ID:** `<session id, or "unknown">`
- **Written at:** `<ISO-8601 UTC, e.g. 2026-09-11T07:20:00Z>`
- **Leases held at exit:** `<paths, or "none — released">`
  <!-- If you exited holding leases, say so. The next agent may need to
       `bin/avcoord reap` an expired one rather than assume it is free. -->

## Target Branch & Worktree Path
- **Branch:** `<branch>`
- **Worktree path:** `../<repo>.<branch>` <!-- relative to the main checkout -->
- **Base / merge target:** `main`
- **Last checkpoint commit:** `<short sha> <subject>`
- **Working tree at exit:** `<clean | dirty — list uncommitted files and why>`

## Completed Sub-actions
<!-- Only things that are DONE and VERIFIED. This list is what the next agent
     is told not to redo — a wrong entry here causes silent regression. -->
- [x] <action> — verified by `<command or test name>`
- [x] <action> — verified by `<command or test name>`

## Unfinished / Failing Items
- [ ] <what is not done, or what is failing and how it fails>
- [ ] <include the actual assertion / exception text, not a paraphrase>

## Reproducer Command
<!-- The single most valuable field. Must be copy-pasteable, must run from the
     worktree root, and must currently FAIL (or reproduce the symptom). -->
```bash
<the exact command that reproduces the failure, e.g. your test runner
scoped to one failing test>
```
- **Expected observation:** `<the exact failure — "AssertionError: expected 3, got 0">`
- **Runtime:** `<approx seconds, so the next agent knows if it hung>`

## Blockers / Critical Context for Next Agent
<!-- Reasoning that is NOT recoverable from the diff. Dead ends are as valuable
     as progress: they stop the next agent re-walking them. -->
- **Blocker:** <what is actually in the way>
- **First thing to do:** <one concrete action — usually: run the reproducer>
- **Prime suspect:** `<file.py:line>` — <why you suspect it>
- **Already tried and rejected:** <approach> — <why it failed>
- **Invariants in play:** <e.g. C7 — touches a projection; correct the journal instead>
- **Do NOT:** <anything that looks tempting but will break something else>

## Verification bar for "done"
- [ ] Reproducer command now passes
- [ ] Full suite green (see `.agentvault/INDEX.md` for this project's command)
- [ ] No unrelated files modified
- [ ] Board updated to `READY_FOR_REVIEW`
