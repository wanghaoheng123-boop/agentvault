---
version: 2.0
type: coordination_contract
status: active
last_updated: 2026-09-09T00:42:00+08:00
---

# Structured task contract

The hash-chained journal is the authority for managed task state. Markdown sessions,
`CURRENT.md`, `board.md`, SQLite slots, and handoff files are views or evidence. They do not
advance a task by themselves.

Create a managed task with one `task.created` event. Its body contains:

- `contract_version: "2.0"`, `task_id`, `status: "proposed"`, and a concrete `objective`;
- `owner_run_id` and an independent `reviewer` principal;
- bounded `read_paths` and `write_paths`, plus `confidentiality`;
- `dependencies` and SHA-256-bound `input_artifact_hashes`;
- an `output` path/schema and exact `acceptance_commands`;
- explicit `assumptions`, `stop_conditions`, and a non-negative `retry_budget`.

Every task append uses journal-head CAS and the task's current revision. The lifecycle is
`proposed → ready → claimed → in_progress → review → verified → integrated`. A task may move
to `blocked` or `cancelled` only along the transitions enforced by the writer. A blocked task
can only resume at `ready`, where dependencies and assumptions are checked again.

`verified` requires evidence references and the hash of a committed `review.receipt` event. The
receipt binds an `ACK` decision to the exact task revision, task-state hash, and evidence list;
the guarded writer verifies that its run is not the owner run and its principal is the
contract's declared reviewer. A path, URL, or self-asserted receipt string is rejected.
`integrated` requires evidence from the combined integration gate. Terminal tasks cannot be
reopened.

Pre-v2 `task.transition` records remain readable so historical journal events stay valid. New
work should use `task.created`; an uncontracted historical task does not gain a v2 verification
claim merely because its projection says `done`.

Handoffs preserve the existing four vectors and add the journal task ID/revision, evidence
references, open assumptions, and expected revision. A receiving principal validates those
references before acknowledging the handoff.

## CLI

All mutation commands require a registered run, its token, and live leases covering the commit
journal plus the affected projection. Create contracts and workspace contexts from bounded JSON
files; advance states with explicit evidence fields:

```sh
bin/avcoord task create --file task.json --agent orchestrator --run "$AVCOORD_RUN_ID"
bin/avcoord task transition --task-id TASK-123 --status review \
  --agent orchestrator --run "$AVCOORD_RUN_ID"
bin/avcoord task review --task-id TASK-123 --decision ACK --evidence-ref proof://gate/1 \
  --agent peer_reviewer --run "$AVCOORD_REVIEW_RUN_ID"
bin/avcoord workspace set --file context.json --agent orchestrator --run "$AVCOORD_RUN_ID"
bin/avcoord verify --agent orchestrator --run "$AVCOORD_RUN_ID" \
  --note "checked task revisions and gate receipts"
```

`task show`, `task list`, and `workspace show` are read-only. `refresh --views-only` never writes
CURRENT. A journal-authority refresh can redraw CURRENT under its lease, but it cannot change the
context revision or `last_verified_at`; only accepted workspace events can do that.
