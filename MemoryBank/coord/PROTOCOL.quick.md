---
version: 1.1
priority: P0
last_updated: 2026-09-06T22:30:00+08:00
type: protocol_quick
---

# PROTOCOL.quick — Iron Laws (P1 on write / multi-agent)

Full detail: `MemoryBank/coord/PROTOCOL.md` · CLI: `python3 scripts/coord/avcoord.py` or `bin/avcoord`

Enforcement: Cursor `.cursor/hooks.json` (PreToolUse) + optional `git config core.hooksPath .githooks`. Agent id: `AVCOORD_AGENT` (default `orchestrator`).

1. **Claim before contested writes** — `claim --agent <id> --resource <path>`; never skip a denied claim.
   A lease covers that path and everything **under** it. It never covers the path's **parent**,
   and never a **sibling sharing a name prefix** (`.../foo` does not cover `.../foobar.md`).
   Aliases (`./x`, `x//y`, `a/../x`, case variants, symlinks) resolve to the same resource, so
   they cannot take separate leases. `..` escapes, absolute paths outside the root, and globs
   other than a trailing `/` or `/**` are **rejected**, not guessed at.
2. **Side effects before ack** — ledger append / file writes, then `ack` / release.
   `release --resource X` only drops leases X **covers**; naming one file no longer deletes a
   lease on the whole subtree containing it.
3. **No peer edits** — never edit another agent’s `mail/*/done` or `agents/<id>/context.md`.
4. **Sessions create-once** — new `sessions/<id>.md`; don’t overwrite peers.
5. **Canonical once** — write canonical path; mirrors derived + checksum.
6. **One SSOT** — AgentVault MemoryBank only; no `workspace/SESSION_STATE.json` AGENT HOOK state here.
7. **IDs via CLI** — `next-id progress|episode|message` before appending ledgers.
8. **Hop cap 8** — non-terminal mail with `hop >= 8` must be `block`.

Contested list: `MemoryBank/coord/contested.json` (edit per project). Defaults include: `CURRENT.md`, `coord/next_ids.json`, `state_tracker.json`, `schema.json`, `index.json`, `docs/manuscript/**`, `build/**`, `output/**`.

9. **Refresh is a view, verification is an act** — `refresh --views-only` regenerates
   `board.md`/`activeContext.md` and needs no lease. `refresh` touches `CURRENT.md` only if you
   hold a lease on it (else exit 2). Clear STALE with `verify --agent <id> --note '<checked>'`,
   which stamps `last_verified_at`. `generated_at` = a view was redrawn; `last_verified_at` =
   someone looked. Thread status is never inferred from prose.

Reads are reads: `status`, `doctor` and `check-lease` never delete leases. Expired leases are
removed only by `avcoord reap` (or implicitly by `claim`).

Glance: `avcoord status` · Health: `avcoord doctor`.

P2 depth/truth: `EXPERTISE.quick.md` (research before conclusions; provenance; peer validation).
