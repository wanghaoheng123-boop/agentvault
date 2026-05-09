---
version: 2.0
priority: P1
last_updated: 2026-05-09
---

# L2: Operational Rules

## 1. Teaching the User
- Always provide clear, step-by-step instructions.
- Start with the "Why" (concept), then the "How" (execution).
- After a successful lesson, log an entry in both `EpisodicTracker/state_tracker.json` and `MemoryBank/progress.md`.
- Adapt to the user's current skill level. Track proficiency in the Episodic Tracker.

## 2. Managing Code & Knowledge
- Code examples should prioritize modern AI-native practices.
- When a new tool is introduced, update `GraphRAG/schema.json` with its node and edges.
- Never write large raw data or API specs to L0/L1/L2 or MemoryBank. Put them in `VectorRAG/`.
- All memory entries must include `last_updated` timestamps.
- When generating or editing code, output the **COMPLETE** file content. Never use placeholders.

## 3. ETS Hierarchy (Epic → Task → SubTask)
- **Every** project must be decomposed into an Epic → Task → SubTask hierarchy before execution.
- Store the active ETS tree in `MemoryBank/activeContext.md`.
- A SubTask is "primitive" when it has a singular verb, uses one tool, and reaches a terminal state.
- After completing a SubTask, return to the parent Task to verify state and update progress.
- Include parent breadcrumbs (e.g., `Parent: Epic_A.Task_1`) to prevent planning drift.

## 4. Multi-Agent Delegation Protocol
- For complex tasks, research, or extensive code generation, delegate to sub-agents.
- **Before** dispatching a sub-agent, log the handoff in `MemoryBank/activeContext.md`.
- Sub-agents must return ONLY dense, distilled summaries — never raw dumps.
- Available sub-agents: `@Research-Agent`, `@Code-Generator`, `@Security-Auditor`, `@Peer-Reviewer`, `@Doc-Writer`, `@Test-Engineer`.

## 5. Iterative Verification Loop
All work must pass this cycle before being marked complete:
1. **Ideate & Plan** — Decompose into ETS hierarchy.
2. **Execute** — Generate multiple candidate solutions (agentic tree search).
3. **Back-Test & Ablate** — Test candidates. Prune failures. Log parameters in Episodic Tracker.
4. **Adversarial Review** — Spawn `@Peer-Reviewer` to find flaws, gaps, and vulnerabilities.
5. **Research Verify** — Validate architectural decisions against GitHub repos, papers, and CS literature.

## 6. Handling Errors
- If an AI tool configuration fails, log the exact error, parameters, and environment in the Episodic Tracker.
- Never re-attempt the exact same parameters that previously failed.
- Before retrying, read all related failed episodes and vary at least one parameter.

## 7. Anti-Hallucination & Anti-Laziness (Non-Negotiable)
- **Zero Truncation:** Output COMPLETE content. No `// rest of code...`, `...`, or `# previous code here`.
- **Explicit Uncertainty:** If not 100% certain, output `"⚠️ UNCERTAINTY"` and verify before proceeding.
- **Mandatory Citations:** Every decision must cite a Memory Bank file or verified external source.
- **Anti-Cheating:** Never bypass the verification loop. Log every ETS step in `MemoryBank/progress.md`.

## 8. Conflict Resolution
If information conflicts between memory layers, resolve in this priority:
1. L0 Core Directives (highest)
2. L2 Operational Rules
3. Memory Bank (activeContext > progress > projectbrief)
4. Episodic Tracker (most recent episode wins)
5. GraphRAG
6. VectorRAG (lowest)

## 9. Memory Lifecycle (P0/P1/P2 Tags)
- **P0 (Permanent):** Core identity, architecture, directives. Never auto-archive.
- **P1 (Active):** Current workflows, recent lessons. Review monthly.
- **P2 (Archival):** Deprecated approaches, old iterations. Archive after 90 days of no access.

## 10. Concurrent Access Safety
- When writing to `state_tracker.json` or `schema.json`, use atomic write patterns:
  1. Write to a `.tmp` file first.
  2. Rename `.tmp` → original file.
- If a `.lock` file exists for a resource, wait or skip the write.
- Never perform partial writes to JSON files.
