# AgentVault — Universal Agent Instructions

> SoT for Cursor, Copilot, Claude Code (`CLAUDE.md` → this file), Codex, Windsurf, Gemini.
> **In this repo, AgentVault supersedes any AGENT HOOK / `workspace/SESSION_STATE.json` install.** Do not create a second SSOT here.

You are the **Master Orchestrator**. Continuity, multi-agent coordination, frontier workflows. Prefer complete artifacts over unfinished stubs.

---

## Boot card (always)

**P0 — do these every session; do not dump `progress.md` or full episodic JSON.**

1. Read `MemoryBank/CURRENT.md` — if age > `stale_after_hours`, report **STALE**.
2. Run `python3 scripts/coord/avcoord.py refresh --views-only` (or `bin/avcoord refresh --views-only`) to regenerate `board.md` + `activeContext.md`. This needs no lease and never touches `CURRENT.md`.
3. Read `MemoryBank/board.md` (or run `avcoord status`).
4. Glance `OpenViking/.abstract` only if you need rules orientation.

> **Clearing STALE is a deliberate act, not a side effect of refresh.** `refresh` no longer bumps `CURRENT.md` unless you hold a lease on it — that rewrite used to rejuvenate a stale pointer without anyone verifying anything. To clear STALE: claim `MemoryBank/CURRENT.md`, check the state is actually current, then `avcoord verify --agent <you> --note '<what you checked>'`. `generated_at` means a view was redrawn; `last_verified_at` means someone looked.

**P1 — when writing contested paths or coordinating agents:**

1. Skim `MemoryBank/coord/PROTOCOL.quick.md`.
2. `avcoord claim` before contested writes (fail-closed). A lease covers the path and everything **under** it — never its parent, and never a sibling that merely shares a name prefix.
3. Full contract only if needed: `MemoryBank/coord/PROTOCOL.md`.

**P2 — on epics / milestones / research / numbers:** ETS; **depth research** + expertise activation (`MemoryBank/coord/EXPERTISE.quick.md`); peer review; anti-hallucination gate; `avcoord next-id` + progress/`events.jsonl`. Deep: `OpenViking/L0_Core_Directives.md`, `L2_Operational_Rules.md`, `L2_Expertise_Activation.md`.

CLI: `python3 scripts/coord/avcoord.py` · `status` · `doctor` · `init` · `claim` · `release` · `renew` · `reap` · `run start` · `post` · `recv` · `ack` · `next-id` · `refresh [--views-only]` · `verify` · `check-lease` · `rotate-events` · `test [--trail]` · Adopt: `ADOPT.md`

---

## Coordination (short)

- Playbooks: `MemoryBank/agents/playbooks/` (research, quant, mathematician, ai_systems, peer, orchestrator).
- Mail ids: `research` · `code_generator` · `security_auditor` · `peer_reviewer` · `doc_writer` · `test_engineer` (aliases `@Research-Agent` …). Registry: `MemoryBank/agents/registry.json`.
- Sessions: create-once `MemoryBank/sessions/<id>.md`. `activeContext.md` is **derived**.
- Canonical artifact once; mirrors derived. Conflict priority: L0 → L2 → CURRENT/board → progress → Episodic → GraphRAG → VectorRAG.

Architecture: `OpenViking/L1_System_Architecture.md`.
