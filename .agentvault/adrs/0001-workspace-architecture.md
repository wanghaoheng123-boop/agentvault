# ADR-0001 — Git Worktrees + AgentVault State Machine for Agent Concurrency & Handoff

- **Status:** Accepted
- **Deciders:** repository owner

## Context

This workspace is worked by multiple autonomous coding agents, often across
sessions that end abruptly (context exhaustion, interruption, a failed gate).
Two problems recur:

1. **Physical collision.** Agents editing one checkout stomp each other's
   uncommitted work. A test run by agent A observes agent B's half-finished
   edit and fails for reasons unrelated to either task.
2. **Cognitive loss.** When a session ends mid-task, the reasoning — what was
   tried, what broke, which test reproduces it — dies with the context window.
   The next agent re-derives it, or worse, redoes finished work and regresses it.

The coordination CLI (`bin/avcoord`) already solves *serialization* well: TTL
leases, an append-only journal, a maildir, and a deterministic gate. But it
assumes a single working tree, and its checkpoint artifact is a session summary
rather than a task-resumption manifest keyed to a branch.

## Decision

Adopt a two-layer architecture with a strict split of responsibility.

**Layer 1 — Physical isolation: one git worktree per branch.** Each task gets
its own branch and checkout at a predictable path. Git's native refusal to
check out one branch in two worktrees becomes the enforcement mechanism for
"one agent per task" — no lock file, no way to defeat it accidentally.

**Layer 2 — Shared cognition: one `.agentvault/`, symlinked into every
worktree.** Code is isolated; *state about the work* is deliberately global.
The `post-create` hook symlinks the canonical `.agentvault/` into each new
worktree, so the board, handoffs, invariants and lessons are one copy seen live
by every agent.

`NEEDS_CONTINUATION` is the load-bearing board state. Entering it is mandatory
before yielding control, and it requires a handoff carrying a **reproducer
command** — the field that lets the next agent verify the failure state in
seconds instead of reconstructing it.

**Relationship to the coordination engine:** `bin/avcoord` is retained, not
replaced. It stays authoritative for leases, the journal, and the deterministic
gate. `.agentvault/` is authoritative for task routing and handoff. To keep
this from becoming a dual source of truth, one hard rule applies:
**`.agentvault/MemoryBank/` must never exist** — the health check rejects that
nesting.

## Consequences

**Positive**

- An interrupted task is resumable by a cold agent with no conversation
  history: branch → worktree path → reproducer → blocker, all on disk.
- Concurrent agents cannot corrupt each other's working files.
- A gotcha learned in one worktree is visible in all of them immediately.
- Handoffs are committed, so continuity survives machine restarts.

**Negative / accepted costs**

- The board becomes a contested shared file across worktrees. Mitigated by C8
  and C9 in the concurrency invariants and by the locking board writer.
- Two systems now describe coordination; the split above is the mitigation, but
  it is real overhead.
- Disk cost: one full checkout per active branch.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Single tree + TTL leases only | Solves write serialization, not concurrent *editing*; a dirty tree still breaks another agent's test run |
| Separate clones per agent | Loses the shared object store and cheap branch switching; the shared-state symlink becomes a cross-clone path problem |
| Session-keyed handoffs in the coordination tree | A resuming agent knows its branch, not the prior session id |
