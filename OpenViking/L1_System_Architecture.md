---
version: 2.0
priority: P0
last_updated: 2026-05-09
---

# L1: System Architecture

## Memory Layers

This workspace utilizes a **Multi-Agent Hybrid Memory Architecture** with five distinct layers. All agents operating here MUST respect these layer boundaries:

### 1. OpenViking (Hierarchical Rules — Static)
**Location:** `OpenViking/`
**Purpose:** Static documentation, overarching architecture, rigid rules, and identity.
**Action:** Always read `.abstract` first. Then L0 for identity, L1 for architecture, L2 for operational rules.
**Mutability:** Rarely modified. Changes require updating `.abstract` index.

### 2. Memory Bank (Project State — Semi-Volatile)
**Location:** `MemoryBank/`
**Purpose:** Project-level state management for cross-device, cross-agent continuity.
**Files:**
| File | Purpose | Mutability |
|------|---------|------------|
| `projectbrief.md` | Core mandate, target audience, objectives | Set once per project |
| `productContext.md` | User experience, problem domain, functional goals | Set once per project |
| `systemPatterns.md` | Architecture, design patterns, dependency maps | Updated per major decision |
| `techContext.md` | Tech stack, environment, formatting constraints | Updated per stack change |
| `progress.md` | **Append-only** historical ledger of completed work | Append after every milestone |
| `activeContext.md` | **Volatile** — current task, last action, next steps | Updated before/after every action |

**Action:** Read `activeContext.md` first when resuming a session. Read `progress.md` to understand history. NEVER delete entries from `progress.md`.

### 3. GraphRAG (Relational Logic Layer)
**Location:** `GraphRAG/`
**Purpose:** Complex dependencies between AI concepts, tools, and workflows.
**Action:** Query when connecting multiple technologies. When adding new tools, always add both a node AND its relationship edges. Include `created_at`, `tags`, and `priority` on all entries.

### 4. Episodic State Tracker (Time-Series Iteration Memory)
**Location:** `EpisodicTracker/`
**Purpose:** Machine-readable chronological log of all learning episodes, tested workflows, and outcomes.
**Action:** Query BEFORE attempting any complex task. Record results to prevent repeating failures. Always include `parameters_tested` and `status` enum (`success`|`failed`|`in_progress`|`blocked`).
**Difference from `progress.md`:** Progress is a human-readable append-only ledger. The Episodic Tracker is machine-readable JSON with structured fields for automated querying.

### 5. Vector RAG (External Reference Layer)
**Location:** `VectorRAG/`
**Purpose:** Heavy, unstructured reference materials (papers, readmes, APIs).
**Action:** Check `index.json` for catalog. Store papers in `papers/`, API docs in `api_docs/`, repo readmes in `repos/`. Never load into context unless explicitly necessary.

## Agent Detection Files

| File | Tool | Purpose |
|------|------|---------|
| `AGENTS.md` | Universal (Codex, Copilot, Windsurf) | Primary instructions — source of truth |
| `CLAUDE.md` | Claude Code | Symlink → `AGENTS.md` |
| `.cursorrules` | Cursor | Cursor-specific overrides only |

## Layer Interaction Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    AGENTS.md (Entry Point)                    │
│                 Read by ALL agents on entry                  │
└────────┬──────────────┬──────────────┬──────────────┬───────┘
         │              │              │              │
    ┌────▼────┐   ┌─────▼─────┐  ┌────▼────┐  ┌─────▼─────┐
    │OpenViking│   │MemoryBank │  │GraphRAG │  │ Episodic  │
    │ (Rules)  │   │ (State)   │  │(Relations│  │ (History) │
    │ L0/L1/L2 │   │ 6 files   │  │  nodes/ │  │  JSON log │
    └──────────┘   └───────────┘  │  edges) │  └───────────┘
                                  └─────────┘
                                       │
                                  ┌────▼────┐
                                  │VectorRAG│
                                  │ (Heavy  │
                                  │  Refs)  │
                                  └─────────┘
```
