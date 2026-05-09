# Contributing to AgentVault

Thank you for your interest in contributing! This project is a community-driven framework for building persistent AI agent memory.

## How to Contribute

### 1. Fork & Clone
```bash
git clone https://github.com/YOUR_USERNAME/agentvault.git
cd agentvault
```

### 2. Types of Contributions Welcome

| Type | Description |
|------|-------------|
| 🧠 **New Memory Patterns** | Propose new OpenViking rules, GraphRAG node types, or Episodic Tracker schemas |
| 🔌 **Agent Integrations** | Add support for new AI agents (e.g., Devin, Aider, Continue) |
| 📚 **VectorRAG Content** | Curate high-quality papers, API docs, or repo analyses |
| 🐛 **Bug Reports** | Report issues with schema validation, symlink compatibility, etc. |
| 📝 **Documentation** | Improve tutorials, examples, or translations |

### 3. Submission Guidelines
- Follow the existing file naming conventions and YAML frontmatter format.
- All JSON files must be valid (run `python3 -c "import json; json.load(open('FILE'))"` to verify).
- Update `OpenViking/.abstract` if you modify any L0/L1/L2 files.
- Update `GraphRAG/schema.json` if you introduce a new concept or tool.
- Log your contribution in `EpisodicTracker/state_tracker.json`.

### 4. Code of Conduct
Be respectful. Be constructive. Help each other learn.
