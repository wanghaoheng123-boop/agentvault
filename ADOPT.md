# Adopt AgentVault in 5 minutes

Portable, git-native multi-agent memory for **any** project. MemoryBank is the only SSOT.

## Install

```bash
# From this repo (or after cloning agentvault)
python3 /path/to/agentvault/scripts/coord/avcoord.py init --target /path/to/your-project --full

# Or copy the template tree
cp -R templates/agentvault-portable/. /path/to/your-project/
```

## First session

```bash
cd /path/to/your-project
bin/avcoord doctor
bin/avcoord status
```

1. Edit `MemoryBank/projectbrief.md` and `MemoryBank/techContext.md`.
2. Open the project in Cursor / Copilot / Claude Code — agents read `AGENTS.md` Boot.
3. Optional enforcement: `git config core.hooksPath .githooks`
4. Contested writes: `bin/avcoord claim --agent orchestrator --resource MemoryBank/CURRENT.md`

## Boot (every agent, every session)

**P0:** `MemoryBank/CURRENT.md` + `board.md` (or `avcoord status`). Do **not** dump `progress.md` or episodic JSON.  
**P1 on write:** `PROTOCOL.quick.md` + claim.  
**P2 on epic:** ETS + `next-id` + append progress/`events.jsonl`.

## Do not install

- AGENT HOOK `workspace/SESSION_STATE.json` (dual SSOT — doctor FAIL)
- Second memory systems as co-equal canonical state (Mem0/UMP may be *bridges* only)

## Episodic hygiene

Prefer append-only `EpisodicTracker/events.jsonl` (HOT). When large:

```bash
bin/avcoord rotate-events --keep 50
```

Archives land in `EpisodicTracker/archive/`. Lease-gate any rewrite of `state_tracker.json`.

## Verify

```bash
bin/avcoord test --trail   # if you keep the test suite
bin/avcoord doctor
```

## Expertise activation (research / quant / anti-hallucination)

On epics and claim-heavy work, agents follow **P2 expertise gates** (not every boot):

1. `MemoryBank/coord/EXPERTISE.quick.md`
2. Domain playbook under `MemoryBank/agents/playbooks/`
3. `ANTI_HALLUCINATION_GATE.md` before COMPLETE
4. Quant work: `QUANT_DESK_PROTOCOL.md`

This keeps cold-start small while forcing depth research when it matters.
