# AgentVault

MemoryBank is the only SSOT. Never create `workspace/SESSION_STATE.json`, `.agentvault/`, or `MemoryBank/active/`.

## Boot (every session)

1. Read `MemoryBank/CURRENT.md` — if age > `stale_after_hours`, report **STALE**.
2. `bin/avcoord refresh --views-only` then read `MemoryBank/board.md` (or `bin/avcoord status`).
3. On contested writes: skim `MemoryBank/coord/PROTOCOL.quick.md` → `bin/avcoord claim`.
4. Code is **DONE** only when `bin/avcoord gate` exits `0` (not chat claims; `verify` only stamps CURRENT).
5. Handoff: `bin/avcoord checkpoint` (4-vector). Gates: `MemoryBank/coord/EXECUTION_GATES.md`.

Progressive disclosure only: OpenViking, full PROTOCOL, EXPERTISE, `WORKSPACE_INDEX.md` when needed. CLI: `bin/avcoord -h`.
