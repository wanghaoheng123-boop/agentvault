# EXECUTION GATES

Lean contract. No same-context role-play. Do not weaken these gates.

## Elevate
Vague prompts → production scope (typed I/O, no silent failures, no stubs). Optional elevated notes: `MemoryBank/coord/specs/<TASK_ID>.md` (never `MemoryBank/active/`).

## Done means exit 0
Conversational COMPLETED is forbidden. Write → `bin/avcoord gate` → fix from traces (doctor + pytest + nav + compileall + import-resolve). `verify` only stamps CURRENT under lease.

## Review without theater
Adversarial review = separate agent/mail or `MemoryBank/coord/reviews/<id>.md` — not Builder/Auditor personas in one window.

## Handoff (4-vector)
`avcoord checkpoint`: goal, status, file:line, next action. CURRENT ≤112 words; resume via checkpoint.

## Monotonic rigor
Upgrades may add gate checks. Never remove tests, dual-SSOT bans, secret redaction, or exit-0 completion.
