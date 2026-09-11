# AgentVault — Central Routing Table

Cognitive hub for multi-agent work in this repository. Every agent reads this
file first, then follows the pointers it needs.

> **Two systems, one name.** `bin/avcoord` + `MemoryBank/` is the coordination
> engine (TTL leases, journal, deterministic gate). `.agentvault/` (this
> directory) is the worktree-oriented task and handoff hub. They are
> complementary, not duplicates. **Never create `.agentvault/MemoryBank/`** —
> `bin/avcoord doctor` rejects that nesting as `DUAL_SSOT`.

---

## 1. Routing table

| I need to… | Go to |
|---|---|
| Know the operating law for agents | `AGENTS.md` (repo root; `CLAUDE.md` points here) |
| See who is working on what | `.agentvault/tasks/board.json` |
| Pick up abandoned work | `.agentvault/handoffs/<branch>.md` |
| Start a handoff record | `.agentvault/handoffs/TEMPLATE.md` |
| Check a rule before changing code | `.agentvault/invariants/` |
| Understand why the workspace is shaped this way | `.agentvault/adrs/0001-workspace-architecture.md` |
| Avoid a known trap | `.agentvault/memory/lessons-learned.md` |
| Configure worktree lifecycle | `.config/wt.toml` |
| Use the coordination CLI | `bin/avcoord -h` |

## 2. Division of responsibility

| Concern | Owner |
|---|---|
| Task routing, branch handoff, continuity | `.agentvault/` |
| TTL leases on contested paths | `bin/avcoord claim` |
| Append-only journal and projections | `MemoryBank/coord/` |
| Deterministic DONE check | `bin/avcoord gate` |
| Session state pointer | `MemoryBank/CURRENT.md` |

Adopters: edit `MemoryBank/coord/contested.json` to declare your project's
multi-writer hot paths, and run `git config core.hooksPath .githooks` to
activate the commit-time lease enforcement floor.

## 3. Board state machine

```
UNASSIGNED ─► IN_PROGRESS ─► READY_FOR_REVIEW ─► MERGED
                  │  ▲
                  ▼  │
     NEEDS_CONTINUATION / BLOCKED
```

`NEEDS_CONTINUATION` is load-bearing: entering it is mandatory before an agent
yields control, and it requires a matching `handoffs/<branch>.md` carrying a
reproducer command. Update the board only through `.agentvault/bin/av-board.py`,
which writes atomically under a lock.

## 4. Adapting this hub to your project

The invariants ship with the rules that are true for any multi-writer agent
workspace. Two files need your project's specifics before they are useful:

- `.agentvault/INDEX.md` — add a "repository shape" section naming your
  runtime, test runner, linters, and the exact command that runs your suite.
- `.agentvault/memory/lessons-learned.md` — starts nearly empty by design.
  Fill it as you learn; an entry is worth writing the moment it costs you
  twenty minutes.
