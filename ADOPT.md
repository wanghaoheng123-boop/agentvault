# Adopt AgentVault in 5 minutes

Portable, git-native multi-agent memory for **any** project. MemoryBank is the only SSOT.

## Install

```bash
# From a cloned AgentVault 2.1 release
python3 /path/to/agentvault/scripts/coord/avcoord.py init --target /path/to/your-project --full
```

## First session

```bash
cd /path/to/your-project
bin/avcoord doctor
bin/avcoord status
```

1. Edit `MemoryBank/projectbrief.md` and `MemoryBank/techContext.md`.
2. Open the project in Cursor / Copilot / Claude Code — agents read `AGENTS.md` §1
   Workspace Entry Protocol.
3. Optional enforcement: `git config core.hooksPath .githooks`
4. Register a run before journal writes, then use its printed `run_id` and `run_token`.
5. Claim every contested target before writing it.
6. Record your project's test command in `.agentvault/INDEX.md`, and adapt the
   test and lint steps in `.config/wt.toml` before relying on `pre-merge`.

## Entry (every agent, every session)

**P0 — orient:** Read `.agentvault/INDEX.md`. Check `.agentvault/tasks/board.json`
and `.agentvault/handoffs/<branch>.md` for work to continue; if a handoff exists,
run its reproducer command **before** reading code. Then read
`MemoryBank/CURRENT.md`, run `bin/avcoord refresh --views-only`, and read
`board.md` or `bin/avcoord status`. Do not dump `progress.md` or episodic JSON.
**P1 on write:** Read `PROTOCOL.quick.md`, register a run, and claim the exact path.
**Before yielding:** checkpoint commit, write `.agentvault/handoffs/<branch>.md`,
set the board to `NEEDS_CONTINUATION`. See `AGENTS.md` §3 — it is mandatory.
**DONE:** `bin/avcoord gate` must exit zero. `verify` records evidence review; it is not a test.

## Upgrade and interrupted install recovery

```bash
python3 /path/to/agentvault/scripts/coord/avcoord.py init --target . --upgrade --preview
python3 /path/to/agentvault/scripts/coord/avcoord.py init --target . --upgrade

# If doctor reports INSTALL_RECOVERY_REQUIRED:
bin/avcoord init --target . --recover resume --preview
bin/avcoord init --target . --recover resume
# or restore the retained before-image:
bin/avcoord init --target . --recover rollback
```

`AGENTVAULT_INSTALL.json` is the software receipt. It does not replace MemoryBank or store task
state. See `MemoryBank/coord/specs/CORE_CONSUMER_CONTRACT.md` and `TASK_CONTRACT.md`.

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
bin/avcoord gate --full
bin/avcoord doctor
```

## Expertise activation (research / quant / anti-hallucination)

On epics and claim-heavy work, agents follow **P2 expertise gates** (not every boot):

1. `MemoryBank/coord/EXPERTISE.quick.md`
2. Domain playbook under `MemoryBank/agents/playbooks/`
3. `ANTI_HALLUCINATION_GATE.md` before COMPLETE
4. Quant work: `QUANT_DESK_PROTOCOL.md`

This keeps cold-start small while forcing depth research when it matters.
