# Invariant: Concurrency & Data Integrity

**Status:** Absolute. This workspace is explicitly multi-writer: several
agents, in several worktrees, mutating one shared state tree with no database
to arbitrate.

## C1 — Mutation of shared state requires a lease

```bash
bin/avcoord claim   --agent <agent> --resource <path>   # acquire
bin/avcoord renew   --agent <agent>                     # extend before TTL
bin/avcoord release --agent <agent> --resource <path>   # release when done
```

- Leases are **TTL-based**. A long task must `renew`.
- Expired leases are removed **only** by `bin/avcoord reap`. Never delete a
  lease file by hand — that is a silent steal.
- Live leases and run registrations are gitignored runtime state, never history.

## C2 — Run binding fails in a way that looks like something else

Leases can be bound to a run. When they are, the pre-commit hook passes
`--run "$AVCOORD_RUN_ID"` — and if that variable is unset, the check fails as
though **no lease existed**.

```bash
export AVCOORD_AGENT=<agent> AVCOORD_RUN_ID=<id> AVCOORD_RUN_TOKEN=<token>
```

If a commit is rejected for a path you know you hold, check these exports
before touching the lease. Some journal operations require leases on more than
the obvious file — claim each path the error names, one at a time.

## C3 — All writes are atomic; no partial files

The convention is **write-temp → `os.replace`**.

- `os.replace` on the same filesystem is the transactional boundary.
- `*.tmp`, `*.bak`, `*.lock` are gitignored so half-written files never enter
  history.
- Never `open(path, "w")` directly on a shared state file — a reader in another
  worktree can observe the truncation.

## C4 — Append-only logs append one whole line, under a lock

- One record = one `write` call under `O_APPEND` + `flock`. Never build a line
  across multiple writes.
- **Never rewrite or delete existing journal lines.** History is corrected by
  appending a correction. Archival is `bin/avcoord rotate-events`, nothing else.

## C5 — Compare-and-swap for slot state

`bin/avcoord intent` supports slot CAS with journal rebase. When two agents may
target the same slot, use the CAS path — a blind read-modify-write loses the
other agent's intent with no error.

## C6 — Derived stores are never authoritative

Local databases and caches built from the journal are **regenerable
projections** and are gitignored. Never treat one as the source of truth;
rebuild it from the journal. Never commit it or its sidecars.

## C7 — Projections are derived, never hand-edited

A projection carries the journal sequence and hash it was built from. Editing
one directly — even to fix an obvious typo — breaks that binding and the health
check reports tampering. **Correct the journal, then re-project.** If a tool is
unavailable in your context, hand off rather than hand-stamping the file.

## C8 — One branch, one worktree

Git refuses to check out the same branch in two worktrees. That is a feature:
it is the physical guarantee behind "one agent per branch".

- Do not defeat it with `--force`, detached HEAD, or a second clone.
- To take over an abandoned branch, use its **existing** worktree.
- Canonical `.agentvault/` state is shared through Git common-directory resolution, so `board.json` is a
  contested file: re-read before writing if you have been idle.

## C9 — Idempotency

Any operation an agent may retry after a crash must be safe to run twice.
Prefer allocating ids via `bin/avcoord next-id` over inventing them, and prefer
"set to state X" over "increment".

---

### Checklist before writing shared state

1. Do I hold a live lease for this exact path?
2. Are `AVCOORD_RUN_ID` / `AVCOORD_RUN_TOKEN` exported if the lease is run-bound?
3. Am I writing atomically, or appending one whole line?
4. Did I re-read the file after my last idle period?
5. Is this operation safe if it runs twice?

Resolve the live hub with `python3 -B .agentvault/bin/av-board.py hub` and use
that path for handoffs and notes. Never replace tracked hub directories with
symlinks. Commit shared state from the primary checkout. See ADR-0002.
