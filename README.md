<div align="center">

# 🧠 AgentVault

### Drop-in persistent memory for any AI coding agent.

**One folder. Any agent. Total recall.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Agents](https://img.shields.io/badge/agents-Cursor%20%7C%20Copilot-purple.svg)](#compatibility)

*Give your AI agent a brain that persists across sessions, tools, and devices.*

</div>

---

## The Problem

Every time you start a new chat with an AI coding agent, it forgets everything. Your architecture decisions, failed experiments, project context — **gone**. You waste tokens re-explaining. The agent repeats mistakes you already debugged. Sound familiar?

## The Solution

**AgentVault** is a portable, file-based memory architecture that any AI coding agent can read on startup. Drop it into your project, and your agent instantly knows:

- 🎯 **Who it is** — Its role, capabilities, and operational rules
- 📋 **What you're building** — Project briefs, tech stack, design patterns
- 🧪 **What already failed** — Every tested parameter, every error, every dead end
- 🔗 **How concepts connect** — A knowledge graph of tools and their relationships
- 🔄 **Where you left off** — Volatile handoff state for cross-device continuity

## ⚡ 30-Second Quick Start

```bash
git clone https://github.com/<your-org>/agentvault.git
python3 agentvault/scripts/coord/avcoord.py init --target ~/my-project --full
# or: cp -R agentvault/templates/agentvault-portable/. ~/my-project/

cd ~/my-project
bin/avcoord doctor && bin/avcoord status
```

Open the project in **Cursor**, **GitHub Copilot**, Claude Code, Codex, Windsurf, or Gemini. Agents read `AGENTS.md` Boot (CURRENT + board). Full guide: [`ADOPT.md`](ADOPT.md).

**Do not** install AGENT HOOK `workspace/SESSION_STATE.json` — MemoryBank is the only SSOT.

---

## 🏗️ Architecture

AgentVault uses a **5-layer hybrid memory architecture** inspired by cognitive science and production-grade RAG systems:

```
your-project/
├── AGENTS.md                        ← 🚪 Boot card (SoT)
├── CLAUDE.md                        ← 🔗 Symlink → AGENTS.md
├── .cursor/rules/agentvault.mdc     ← 🎯 Cursor always-on pointer
│
├── OpenViking/                      ← 📜 Rules (L0 identity, L1 architecture, L2 ops)
│   └── .abstract                    ←    TOC — read on need
│
├── MemoryBank/                      ← 💾 Project state + coordination
│   ├── CURRENT.md                   ←    Single-writer “what’s next”
│   ├── board.md                     ←    Dashboard (leases, mail, threads)
│   ├── activeContext.md             ←    Derived compat summary (not SSOT)
│   ├── sessions/                    ←    Create-once handoffs
│   ├── agents/                      ←    Registry + private context
│   ├── coord/                       ←    PROTOCOL.quick + PROTOCOL + leases + mail
│   ├── progress.md                  ←    Append-only ledger
│   └── …
│
├── scripts/coord/avcoord.py         ← 🛠️ Mutation API (+ bin/avcoord)
├── GraphRAG/schema.json
├── EpisodicTracker/events.jsonl     ← Preferred episode stream
└── VectorRAG/                       ← Heavy refs via index.json
```

### How It Works

```
Agent opens project → reads AGENTS.md Boot card
        │
        ▼
MemoryBank/CURRENT.md  (+ STALE check)
        │
        ▼
MemoryBank/board.md  or  avcoord status
        │
        ▼
IF write / multi-agent → PROTOCOL.quick + avcoord claim
        │
        ▼
Deep links only as needed (L0/L2, full PROTOCOL, progress tail, events.jsonl)
        │
        ▼
Execute ✅
```

`activeContext.md` is **derived** by `avcoord refresh` — do not treat it as the handoff bus.

---

## 🔑 Key Features

### 🛡️ Never Repeat Mistakes
The **Episodic Tracker** logs every experiment with `parameters_tested` and `status`. Before trying anything complex, the agent checks what already failed.

### 🔄 Cross-Device, Cross-Agent Continuity
`MemoryBank/CURRENT.md` is the single-writer volatile pointer; session detail lives in `MemoryBank/sessions/`. `activeContext.md` is a derived compat summary. Stop on one device, open on another — boot CURRENT + board + PROTOCOL.

### 📬 Multi-Agent Coordination
Portable file protocol (no server): TTL path leases, maildir messaging, monotonic IDs, blackboard.

```bash
python3 scripts/coord/avcoord.py doctor
python3 scripts/coord/avcoord.py claim --agent research --resource VectorRAG/index.json
python3 scripts/coord/avcoord.py post --from orchestrator --to research --type assign --summary "..."
python3 scripts/coord/avcoord.py refresh
```

Contract: [`MemoryBank/coord/PROTOCOL.quick.md`](MemoryBank/coord/PROTOCOL.quick.md) (full: [`PROTOCOL.md`](MemoryBank/coord/PROTOCOL.md)).

```bash
python3 scripts/coord/avcoord.py status
# or: bin/avcoord status
```

### 🧩 Sub-Agent Delegation
The Master Orchestrator protocol includes a 6-agent registry for delegating work:

| Agent | Specialty |
|-------|-----------|
| `@Research-Agent` | Web search, paper analysis, trend identification |
| `@Code-Generator` | Code writing, refactoring, architecture |
| `@Security-Auditor` | Vulnerability analysis, dependency auditing |
| `@Peer-Reviewer` | Logic gaps, edge cases, adversarial review |
| `@Doc-Writer` | Documentation, README, API specs |
| `@Test-Engineer` | Test cases, ablation studies, regression |

### 📐 ETS Hierarchy
Every project is decomposed into **Epic → Task → SubTask** before execution. This prevents planning drift and ensures auditable progress.

### 🚫 Anti-Hallucination Guardrails
Scoped by severity (see `AGENTS.md` / L0): prefer complete owned artifacts (P1); flag uncertainty on facts/APIs (P1); cite sources on epics (P2).

### 🎓 Expertise activation (P2)
Depth research, specialist playbooks (AI systems, quant, mathematician), epistemic labels, and an anti-hallucination gate — loaded on epics, not every cold start. See `EXPERTISE.quick.md` and `ADOPT.md`.

### 📦 VectorRAG / swarm hygiene
- Prefer committing `VectorRAG/index.json` + small canonical papers; leave large draft sprawl and regenerable embeddings untracked (`.gitignore`).

---

## <a id="compatibility"></a>🔌 Compatibility

| Environment | Detection file | Notes |
|---------------|----------------|-------|
| **Cursor** | `AGENTS.md`, `.cursorrules`, `.cursor/rules/agentvault.mdc` | Full support |
| **Claude Code** | `CLAUDE.md` → `AGENTS.md` | Symlink |
| **GitHub Copilot** | `AGENTS.md`, `.github/copilot-instructions.md` | Full support |
| **Windsurf** | `.windsurfrules`, `AGENTS.md` | Thin pointer |
| **Gemini CLI** | `GEMINI.md`, `AGENTS.md` | Thin pointer |
| **Codex CLI** | `AGENTS.md`, `AGENTS.codex.md` | Thin pointer |
| **Other assistants** | `AGENTS.md` | Supported when the assistant loads project-level instruction files |

---

## 🛠️ Configuration Guide

### Step 1: Clone or Copy
```bash
git clone https://github.com/YOUR_USERNAME/agentvault.git
```

### Step 2: Populate Your Project Brief
Edit `MemoryBank/projectbrief.md` with your project's mission, objectives, and constraints.

### Step 3: Set Your Tech Stack
Edit `MemoryBank/techContext.md` with your language, framework, and environment.

### Step 4: Customize Rules (Optional)
Edit `OpenViking/L2_Operational_Rules.md` to add project-specific coding standards.

### Step 5: Open in your assistant
Open the folder in Cursor, GitHub Copilot, or any compatible assistant so it reads `AGENTS.md` and operates with full memory.

---

## 📖 Philosophy

AgentVault is built on three principles:

1. **Files are universal.** Every AI agent can read files. No vendor lock-in, no APIs, no databases required.
2. **Memory should be structured.** Raw context dumps waste tokens. Layered memory (rules → state → graph → history → references) keeps the agent focused.
3. **Agents should evolve.** The Master Orchestrator is authorized to upgrade its own rules and methodologies over time.

---

## 📄 License

[MIT](LICENSE) — Use it, fork it, build on it.

---

<div align="center">

**Built for developers who are tired of re-explaining their codebase to AI.**

⭐ Star this repo if it saved you from repeating yourself.

</div>
