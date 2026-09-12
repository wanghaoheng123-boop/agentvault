# ADR-0002 — Resolve shared state through Git's common directory

- **Status:** Accepted, 2026-09-12
- **Amends:** ADR-0001's directory-symlink mechanism and merge timing

Fresh worktrees already contain the tracked `.agentvault` directory. The old
hook refused that directory; replacing it with a symlink would change tracked
files. Without a working hook, board writers silently used separate ledgers.

Keep runtime helpers and protocol files tracked in each worktree. Resolve live
state to the primary checkout's `.agentvault` using
`git rev-parse --path-format=absolute --git-common-dir`. The board CLI performs
this resolution and exposes `av-board.py hub` for handoffs, lessons and live
invariant reads. Standalone installations without Git use their local hub.
Missing canonical hubs fail instead of falling back to a second authority.
Canonical state changes must be committed from the primary checkout.

This change does not migrate the legacy avcoord kernel to shared worktree
storage. Its CLI still binds state to its repository root. Lifecycle hooks
therefore invoke the primary checkout's coordinator for global hub leases.
Agents doing global coordination must do the same.

Worktrunk 0.76 uses top-level hook names. `pre-start` registers tasks before
agent execution. `pre-merge` runs tests and the independent invariant audit
without closing tasks. `post-merge` verifies source-commit ancestry in the target,
then archives the handoff and records merge provenance under the canonical lease
and board lock. Repeating completion does not duplicate the provenance entry.
Use `wt merge main --no-squash --no-rebase --no-remove` so that the checked source
commit remains in target history and cleanup remains an explicit decision.

Worktrunk's default sibling worktree layout is sufficient. The old `PORT`
scaffold is removed because no service consumes it. Worktree layout belongs in
Worktrunk user configuration, not unsupported project `[templates]` tables.

Validation uses disposable real Git worktrees, concurrent board writers, missing
state, active-owner refusal, and unmerged-commit refusal. Hook discovery is
checked with `wt hook show --format json`. The release smoke verifies standalone
installation separately; this does not claim an AI audit ran inside a unit test.

References: [Worktrunk configuration](https://worktrunk.dev/config/) and
[hook lifecycle](https://worktrunk.dev/hook/).
