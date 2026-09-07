---
version: 1.0
priority: P1
last_updated: 2026-09-05T14:30:19+08:00
---

# L2: Expertise Activation Layer

> Complements `L2_Operational_Rules.md`. Keeps P0 boot slim; activates depth on P2.

## Why this layer exists
Prompts that say “you are an expert” without tools, retrieval, or verification **increase confident error**. Industry evidence (2025–2026): hallucinations are contained by **structure outside the LLM loop** — hooks, Graph-RAG, cross-agent validation, provenance — not by longer sermons.

## Severity
- **P0:** Do not load this file every session.
- **P2-on-epic / research / numbers / novel domains:** Orchestrator MUST open `EXPERTISE.quick.md` and run depth research.

## Expertise activation (MUST)
1. **Select persona + playbook** — Map task → agent id → `MemoryBank/agents/playbooks/<role>.md`.
2. **Load skills** — Prefer global `~/.cursor/skills/` (e.g. academic-deep-research, citation-verification-gate). Project playbooks point; do not paste skill bodies into boot.
3. **Retrieve before reason** — Check GraphRAG schema edges, VectorRAG `index.json`, prior episodes (query, don’t dump).
4. **Depth research gate** — If task is non-trivial: spawn research (parallel avenues OK) → distill → then implement/write.
5. **Epistemic discipline** — Label claims; numeric provenance; UNCERTAINTY halt.
6. **Validate with another agent** — Executor generates; peer/security/test validates.

## Specialist roster (activation map)

| Need | Agent / playbook | Also load |
|------|------------------|-----------|
| Literature / web / papers | `research` | academic-deep-research, citation-verification-gate |
| Quant research / analytics | `research` + `quant_researcher` playbook | QUANT_DESK_PROTOCOL |
| Math / proofs / formal | `research` + `mathematician` playbook | ANTI_HALLUCINATION_GATE H3/H7 |
| AI agent / prompting design | `research` + `ai_systems` playbook | this file + competitive memos |
| Adversarial truth | `peer_reviewer` | ANTI_HALLUCINATION_GATE |
| Security / injection | `security_auditor` | hooks > prompts mindset |
| Code implementation | `code_generator` | tests via `test_engineer` |

## Steal vs Avoid

| Steal | Avoid |
|-------|-------|
| Tool-first + hooks (leases, citation gate) | “Be accurate” without verification |
| Cross-agent validator ≠ executor | Self-grade only |
| GraphRAG structured query | Dumping a whole corpus into context |
| HOT episodic parameter logs | Invented backtests |
| Slim boot + progressive disclosure | Ritual overload every turn |
| Epistemic labels + provenance | False precision |

## Depth research trigger (orchestrator)
Fire depth research when **any** is true:
- User asks for research, strategy, quant, math, citations, or “best practice”
- Task touches unknown APIs / markets / papers
- Deliverable will be shared externally or used for decisions
- Prior episodes show related failures

Skip when: typo fix, single known file edit, pure formatting.

## Relation to coordination
Claims/leases/mail unchanged (`PROTOCOL.quick`). Expertise layer governs **how thinking is staffed and verified**, not file locking.
