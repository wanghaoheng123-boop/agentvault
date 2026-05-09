<div align="center">

# 🧠 AgentVault

### Drop-in persistent memory for any AI coding agent.

**One folder. Any agent. Total recall.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Agents](https://img.shields.io/badge/agents-Cursor%20%7C%20Claude%20%7C%20Codex%20%7C%20Copilot%20%7C%20Windsurf-purple.svg)](#compatibility)

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
# Clone AgentVault into your project
git clone https://github.com/YOUR_USERNAME/agentvault.git .agentvault

# Or just copy the folder into any existing project
cp -r agentvault/ ~/your-project/.agentvault/
```

Then open the project in **Cursor, Claude Code, Codex, Copilot, or Windsurf**. The agent auto-detects `AGENTS.md` and boots up with full memory. That's it.

---

## 🏗️ Architecture

AgentVault uses a **5-layer hybrid memory architecture** inspired by cognitive science and production-grade RAG systems:

```
your-project/
├── AGENTS.md                        ← 🚪 Entry point (auto-detected by all agents)
├── CLAUDE.md                        ← 🔗 Symlink → AGENTS.md (Claude Code compat)
├── .cursorrules                     ← 🎯 Cursor-specific overrides only
│
├── OpenViking/                      ← 📜 Layer 1: RULES (static, rigid)
│   ├── .abstract                    ←    Table of contents (read first!)
│   ├── L0_Core_Directives.md        ←    Identity & principles
│   ├── L1_System_Architecture.md    ←    Memory layer definitions
│   └── L2_Operational_Rules.md      ←    Execution protocols
│
├── MemoryBank/                      ← 💾 Layer 2: PROJECT STATE (semi-volatile)
│   ├── projectbrief.md              ←    What you're building
│   ├── productContext.md            ←    Who it's for
│   ├── systemPatterns.md            ←    How it's architected
│   ├── techContext.md               ←    What stack you're using
│   ├── progress.md                  ←    What's been done (append-only!)
│   └── activeContext.md             ←    What's happening RIGHT NOW
│
├── GraphRAG/                        ← 🕸️ Layer 3: KNOWLEDGE GRAPH
│   └── schema.json                  ←    Nodes & edges of concepts/tools
│
├── EpisodicTracker/                 ← 📊 Layer 4: EXPERIMENT LOG
│   └── state_tracker.json           ←    Every attempt, every outcome
│
└── VectorRAG/                       ← 📚 Layer 5: REFERENCE LIBRARY
    ├── index.json                   ←    Catalog of stored materials
    ├── papers/                      ←    Research papers
    ├── api_docs/                    ←    API specifications
    └── repos/                       ←    GitHub repo analyses
```

### How It Works

```
Agent opens your project
        │
        ▼
Reads AGENTS.md (auto-detected)
        │
        ▼
Reads OpenViking/.abstract → L0 → L1 → L2
        │  (understands its identity and rules)
        ▼
Reads MemoryBank/activeContext.md
        │  (understands current state and what's next)
        ▼
Checks EpisodicTracker/state_tracker.json
        │  (avoids repeating past failures)
        ▼
Queries GraphRAG/schema.json as needed
        │  (understands how concepts connect)
        ▼
Executes with full context ✅
```

---

## 🔑 Key Features

### 🛡️ Never Repeat Mistakes
The **Episodic Tracker** logs every experiment with `parameters_tested` and `status`. Before trying anything complex, the agent checks what already failed.

### 🔄 Cross-Device, Cross-Agent Continuity
`activeContext.md` is a volatile handoff file. Stop working on your Mac, open the same folder on your Linux box with a different agent — it picks up exactly where you left off.

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
- **Zero Truncation** — No `// rest of code...` shortcuts
- **Explicit Uncertainty** — Agent must flag when it's not 100% sure
- **Mandatory Citations** — Every decision cites a source

---

## <a id="compatibility"></a>🔌 Compatibility

| Agent | Detection File | Status |
|-------|---------------|--------|
| **Cursor** | `.cursorrules` | ✅ Full support |
| **Claude Code** | `CLAUDE.md` (symlink) | ✅ Full support |
| **OpenAI Codex** | `AGENTS.md` | ✅ Full support |
| **GitHub Copilot** | `AGENTS.md` | ✅ Full support |
| **Windsurf** | `AGENTS.md` | ✅ Full support |
| **Gemini (Antigravity)** | `AGENTS.md` | ✅ Full support |

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

### Step 5: Open in Any Agent
Open the folder in Cursor, Claude Code, or any supported agent. It will automatically read `AGENTS.md` and operate with full memory.

---

## 📖 Philosophy

AgentVault is built on three principles:

1. **Files are universal.** Every AI agent can read files. No vendor lock-in, no APIs, no databases required.
2. **Memory should be structured.** Raw context dumps waste tokens. Layered memory (rules → state → graph → history → references) keeps the agent focused.
3. **Agents should evolve.** The Master Orchestrator is authorized to upgrade its own rules and methodologies over time.

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. All contributions welcome — new memory patterns, agent integrations, documentation, and bug fixes.

---

## 📄 License

[MIT](LICENSE) — Use it, fork it, build on it.

---

<div align="center">

**Built for developers who are tired of re-explaining their codebase to AI.**

⭐ Star this repo if it saved you from repeating yourself.

</div>
