---
version: 1.0
priority: P0
last_updated: 2026-09-05T02:11:00+08:00
type: coordination_protocol
---

# AgentVault Coordination Protocol

Portable multi-agent file coordination for any runtime that can read/write files.
Patterns adapted from OACP (mailboxes), dibs/lockito (TTL leases), pi-ensemble (blackboard + audit), Magentic-One (task vs progress ledgers).

**CLI:** `python3 scripts/coord/avcoord.py <command>`

**Env:** `AVCOORD_ROOT` — optional workspace root override (used by sandboxed `test` / `test --trail`).

## Iron Laws

1. Side effects / ledger append **before** ack / cursor advance / lease release.
2. Never edit another agent’s `mail/*/done` or private `agents/<id>/context.md`.
3. Never overwrite a peer session file; create a new `sessions/<id>.md`.
4. Canonical artifact written once; mirrors only via documented sync (checksum in index).
5. Mandatory boot: `OpenViking/.abstract` → L0 → `MemoryBank/CURRENT.md` → `board.md` → this PROTOCOL (not full `progress.md` / episodes).
6. **One SSOT:** AgentVault MemoryBank. Do **not** create a competing `workspace/SESSION_STATE.json` AGENT HOOK state machine in this repo.

## Volatile State

| File | Writer | Role |
|------|--------|------|
| `MemoryBank/CURRENT.md` | Orchestrator / `avcoord refresh` only | Pointer: session_id, next steps, `stale_after` |
| `MemoryBank/board.md` | Orchestrator / `avcoord refresh` | Blackboard: owners, open threads, contested paths |
| `MemoryBank/activeContext.md` | Derived by `refresh` | Compat summary for older agents |
| `MemoryBank/sessions/<id>.md` | Creating agent once | Create-once handoff; never overwrite peers |

If `CURRENT.md` age exceeds `stale_after` (default 48h), boot must report **STALE** and refresh before work.

## Messaging (Maildir)

Path: `MemoryBank/coord/mail/<agent_id>/{tmp,new,cur,done}/`

Delivery: write JSON to `tmp/` → rename to `new/` → receiver moves to `cur/` → after side effects move to `done/`.

### Message schema

```json
{
  "id": "MSG-000001",
  "ts": "ISO-8601",
  "from": "orchestrator",
  "to": "research",
  "type": "assign",
  "task_id": "TASK-001",
  "hop": 0,
  "max_hop": 8,
  "refs": ["path/or/ref-id"],
  "summary": "dense mandate only",
  "status": "pending"
}
```

### Types (speech acts)

`assign` | `handoff` | `done` | `block` | `review_request` | `review_feedback` | `ack` | `inform`

Terminal acts: `done`, `block`, `ack` (when closing a thread). Max hop 8 — then emit `block`.

Payloads carry **summaries + refs only** (claim-check). Heavy bodies live in VectorRAG / session files.

## Leases (TTL path claims)

Path: `MemoryBank/coord/leases/<hash>.json`

```json
{
  "agent_id": "code_generator",
  "resources": ["docs/manuscript/volume_01/**"],
  "reason": "chapter edit",
  "task_id": "TASK-001",
  "expires_at": "ISO-8601",
  "renewed_at": null,
  "created_at": "ISO-8601"
}
```

- Default TTL: **15 minutes**. Max: **2 hours**. Renew allowed while held.
- Stale leases (past `expires_at`) are reclaimable.
- **Fail-closed:** if claim denied, do not write. Never “skip” a contested path.
- Contested resources: `EpisodicTracker/state_tracker.json`, `GraphRAG/schema.json`, `VectorRAG/index.json`, `MemoryBank/CURRENT.md`, `MemoryBank/coord/next_ids.json`, plus whatever multi-writer globs your project adds.

## ID Allocator

`MemoryBank/coord/next_ids.json` — monotonic counters for `progress`, `episode`, `message`.
Allocate via `avcoord next-id <kind>` under lease. No silent remaps; tombstone skipped IDs in audit if needed.

## Append-Only Ledgers

- `MemoryBank/progress.md` — append entries with allocated Entry #.
- `EpisodicTracker/events.jsonl` — **preferred** append stream for new episodes (always use for new work).
- `EpisodicTracker/state_tracker.json` — legacy bulk store; **lease-gated**; avoid bulk rewrites; freeze unless migrating under claim.
- `MemoryBank/coord/audit.jsonl` — every claim/release/post/ack/doctor event.

## Enforcement Floor

- Cursor: `.cursor/hooks.json` → PreToolUse runs `.cursor/hooks/avcoord-lease-check.py` (`check-lease`).
- Git (optional): `git config core.hooksPath .githooks` → pre-commit blocks contested staged files without a live lease.
- Agent id: env `AVCOORD_AGENT` (default `orchestrator`).
- Glance UI: `avcoord status [--json]`.

## Canonical vs Mirror

1. Choose one **canonical** path (for example a `papers/` or manuscript source tree).
2. Write canonical under lease.
3. Mirrors (`docs/...`) are derived copies; record checksum/path in `VectorRAG/index.json`.
4. Never edit mirror without updating canonical first.

## CLI Quick Reference

```bash
python3 scripts/coord/avcoord.py status [--json]
python3 scripts/coord/avcoord.py claim --agent ID --resource PATH [--ttl 15m] [--reason TEXT]
python3 scripts/coord/avcoord.py release --agent ID [--resource PATH|--all] [--token TOK]
python3 scripts/coord/avcoord.py renew --agent ID [--ttl 15m] [--token TOK]
python3 scripts/coord/avcoord.py reap
python3 scripts/coord/avcoord.py post --from ID --to ID --type assign --summary "..." [--refs a,b]
python3 scripts/coord/avcoord.py recv --agent ID [--full]   # summaries by default; --full for bodies
python3 scripts/coord/avcoord.py ack --agent ID --msg MSG-ID
python3 scripts/coord/avcoord.py next-id progress|episode|message
python3 scripts/coord/avcoord.py check-lease --agent ID --path PATH
python3 scripts/coord/avcoord.py refresh [--views-only] [--agent ID]
python3 scripts/coord/avcoord.py verify --agent ID --note "what you checked"
python3 scripts/coord/avcoord.py doctor
python3 scripts/coord/avcoord.py test [--trail]
# shim: bin/avcoord <same>
```

`release` requires `--all` or `--resource` (fail-closed). All lease mutations — claim, release,
renew, reap — serialize on `leases/.claim.lock` via `flock`. Registry validates `--agent`;
`write_scopes` emit WARN on claim (`--strict-scopes` to fail instead; off by default because the
registry does not yet cover every live research tree).

**Freshness.** `refresh --views-only` regenerates `board.md`/`activeContext.md` and needs no lease.
Plain `refresh` writes `CURRENT.md` only when the caller holds a lease authorizing it, else it
emits the views and exits **2**. `generated_at` means a view was redrawn; `last_verified_at`
(stamped by `verify`) means someone actually checked. Thread status is never inferred from prose —
the old unanchored ``COMPLETE`` regex is gone.

## Managed Task Lifecycle

New coordinated work uses the journal-backed v2 task contract in
`MemoryBank/coord/specs/TASK_CONTRACT.md`. A task begins with `task.created` and follows
`proposed → ready → claimed → in_progress → review → verified → integrated`. `verified`
requires independent-review evidence; `integrated` requires a passing combined gate. Session
notes and conversational completion never change this state. Historical pre-v2 transitions
remain readable but do not receive v2 verification semantics.

## Anti Dual-SSOT

If `workspace/SESSION_STATE.json` exists and is treated as co-equal with MemoryBank, `avcoord doctor` **FAIL**. AgentVault MemoryBank is the only project SSOT here. Platform wrappers must point here, not invent a second state machine.

## References

- OACP: https://oacp.dev/
- dibs: https://github.com/polymatx/dibs
- lockito: https://github.com/TheUnderdev/lockito
- pi-ensemble: https://github.com/sztlink/pi-ensemble/
- Magentic-One: https://arxiv.org/abs/2411.04468
