---
version: 1.0
priority: P2
last_updated: 2026-09-05T14:30:19+08:00
type: expertise_quick
---

# EXPERTISE.quick — Depth & Truth Gates (P2 on epic / research / numbers)

Full: `OpenViking/L2_Expertise_Activation.md` · Quant: `QUANT_DESK_PROTOCOL.md` · Gate: `ANTI_HALLUCINATION_GATE.md`

1. **Depth research before answers** — For non-trivial / novel / numeric / claim-heavy work: spawn `@Research-Agent` (or domain playbook) *before* drafting conclusions. Skip only for trivial single-file edits.
2. **Activate expertise with tools** — Role alone is theater. Load the matching playbook + skills + retrieval (GraphRAG query, VectorRAG index, web/DOI check).
3. **Label epistemic status** — Every material claim: `FACT` | `ESTIMATE` | `OPINION` | `SCENARIO` | `UNCERTAIN`.
4. **Numeric provenance** — Number ⇒ source path/URL + as-of date + unit. **Material figures must come from code/tool execution**, not mental math.
5. **Refuse to guess** — If unverified, output `⚠️ UNCERTAINTY` and verify; never invent citations, APIs, or backtest results.
6. **Cross-agent validation** — Executor ≠ Validator. Peer/security review on P2 milestones before COMPLETE.
7. **Hooks > prompts** — Enforcement (leases, citation gate, tests) beats “be careful” prose.

Playbooks: `MemoryBank/agents/playbooks/`. Skills: prefer `~/.cursor/skills/` (citation-verification-gate, academic-deep-research).
