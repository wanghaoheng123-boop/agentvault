# AgentVault

Follow `AGENTS.md` §1 Workspace Entry Protocol exactly; handoff per §3 before
yielding control. `.agentvault/` owns task routing and handoff; MemoryBank and
`bin/avcoord` own TTL leases, the journal, and the deterministic gate.
Never create `.agentvault/MemoryBank/` or `workspace/SESSION_STATE.json`.
