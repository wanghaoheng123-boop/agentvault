# AgentVault — Gemini CLI

Follow `AGENTS.md` §1 Workspace Entry Protocol exactly. Do not invent a shorter entry.
Handoff on interrupt is mandatory: `AGENTS.md` §3.
Task routing and handoff live in `.agentvault/`; coordination leases and the journal
remain in MemoryBank via `bin/avcoord`. Never create `.agentvault/MemoryBank/`
or `workspace/SESSION_STATE.json`.
