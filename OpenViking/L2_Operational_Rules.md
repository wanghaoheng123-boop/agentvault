---
version: 3.1
priority: P1
last_updated: 2026-09-05T14:00:00+08:00
---

# L2: Operational Rules

Severity (same contract as `AGENTS.md`):
- **P0-always** — STALE check, one SSOT, don’t dump progress/episodic
- **P1-on-write** — claim contested paths; use `avcoord`
- **P2-on-epic** — ETS, peer review, progress Entry + `events.jsonl`

## 1. Teaching the User (P2 when documenting a lesson)
- Provide clear, step-by-step instructions; start with Why, then How.
- After a successful lesson: `avcoord next-id` → append `MemoryBank/progress.md` + **`EpisodicTracker/events.jsonl`** (preferred). Do not bulk-rewrite `state_tracker.json` without a lease.
- Adapt to skill level; query episodes — never dump the whole tracker.

## 2. Managing Code & Knowledge (P1 on shared writes)
- Prefer modern AI-native practices.
- New tools → update `GraphRAG/schema.json` under lease + atomic write.
- Heavy refs / papers → `VectorRAG/` via `index.json`. **Commit policy:** prefer index + small canonical papers; leave large draft sprawl / regenerable binaries untracked (see `.gitignore`).
- All memory entries need `last_updated`. Prefer complete writes for small owned artifacts.

## 3. ETS Hierarchy (P2-on-epic)
- Decompose non-trivial projects into Epic → Task → SubTask before execution.
- Store the active ETS pointer in `MemoryBank/CURRENT.md`; detail in `sessions/<id>.md`.
- `activeContext.md` is **derived** (`avcoord refresh`) — not the handoff bus.
- SubTask is primitive when it has one verb, one tool, and a terminal state.
- Parent breadcrumbs (e.g. `Parent: Epic_A.Task_1`) prevent planning drift.

## 4. Multi-Agent Delegation (P1)
- Claim contested paths → `avcoord post` assign → refresh CURRENT/board.
- Sub-agents return dense summaries via `done`/`handoff` with `refs[]` only.
- Registry: `research`, `code_generator`, `security_auditor`, `peer_reviewer`, `doc_writer`, `test_engineer`.
- Quick laws: `PROTOCOL.quick.md`; full: `PROTOCOL.md`.

## 4b. Expertise & Depth Research (P2)
For epics, research, quant/math, or claim-heavy work: follow `MemoryBank/coord/EXPERTISE.quick.md` and `OpenViking/L2_Expertise_Activation.md`. Activate playbooks under `MemoryBank/agents/playbooks/`. Run `ANTI_HALLUCINATION_GATE.md` before COMPLETE.

## 5. Iterative Verification Loop (P2-on-epic)
1. Ideate & plan (ETS) → 2. Execute candidates → 3. Back-test / ablate → 4. Peer review on milestones → 5. Research-verify architectural claims.

## 6. Handling Errors (P0 for known failures)
- Log exact error + parameters in Episodic Tracker (`events.jsonl`).
- Never retry the exact same failed parameters; query related episodes first.

## 7. Anti-Hallucination & Completeness (scoped)
- **Owned artifacts (P1):** Prefer complete content; no unfinished placeholder stubs.
- **Uncertainty (P1 on facts/APIs):** If not sure, mark uncertainty and verify — don’t invent endpoints or numbers.
- **Citations (P2-on-epic):** Cite Memory Bank or verified external sources for methodological decisions.
- **Anti-cheating (P2-on-epic):** Don’t skip verification on milestones; log ETS steps with allocated Entry #.

## 8. Conflict Resolution
1. L0 → 2. L2 → 3. CURRENT/board > progress > brief (`activeContext` derived) → 4. Episodic → 5. GraphRAG → 6. VectorRAG.

## 9. Memory Lifecycle
- **P0 Permanent** / **P1 Active** / **P2 Archival** (90 days unused).

## 10. Concurrent Access Safety (P1 — enforced)

**Fail-closed. Never skip a contested write.**

1. `avcoord claim --agent <id> --resource <path>` (or `bin/avcoord`)
2. Atomic writes for shared JSON
3. Append ledgers only after `next-id`
4. Release/renew; default TTL 15m (max 120m)
5. `avcoord doctor` / `status` if STALE or corrupt

**Enforcement floor:** Cursor PreToolUse (`.cursor/hooks.json`) + optional git `core.hooksPath=.githooks` run `check-lease` on contested paths. Set `AVCOORD_AGENT` if not `orchestrator`.

### Contested paths
- `MemoryBank/CURRENT.md`, `MemoryBank/coord/next_ids.json`
- `EpisodicTracker/state_tracker.json` (lease-gated; prefer append-only `events.jsonl`)
- `GraphRAG/schema.json`, `VectorRAG/index.json`, `OpenViking/`, `AGENTS.md`
- `docs/manuscript/**`, `build/**`, `output/**`

### Anti Dual-SSOT
Do **not** install `workspace/SESSION_STATE.json` (AGENT HOOK) as co-equal SSOT here. Doctor FAIL if both are treated as SSOT.

### Hygiene
- Prefer `events.jsonl` for new episodes; freeze bulk rewrites of `state_tracker.json`.
