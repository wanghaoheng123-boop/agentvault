# Lessons Learned — Shared Operational Memory

Empirically-earned gotchas for this repository. Each entry: the symptom you
will see, the cause, and the fix. Append new entries; never delete one without
evidence it no longer applies.

This file ships nearly empty **by design** — the entries that matter are the
ones your project earns. Write one the moment a problem costs you twenty
minutes. The seeded entries below hold for any AgentVault workspace.

---

### L1 — Run-bound leases fail the pre-commit hook in a misleading way
**Symptom:** `contested path lacks live lease for <agent>` on a path you hold.
**Cause:** the hook passes `--run "$AVCOORD_RUN_ID"` only when that variable is
set. With a run-bound lease and an unset `AVCOORD_RUN_ID`, the check fails as
though no lease existed. The run token prints **once**, at registration.
**Fix:** export `AVCOORD_AGENT`, `AVCOORD_RUN_ID` and `AVCOORD_RUN_TOKEN`
before committing. Never respond by deleting the lease or using `--no-verify`.

### L2 — One journal operation can need several leases
**Symptom:** you claim the obvious file, and the commit still fails naming a
different path under the coordination tree.
**Cause:** committing a journal event touches the commit log, the projections,
and the id allocator — each separately leased.
**Fix:** claim each path the error names, one at a time, under the same run.
Pre-check every staged path before committing rather than discovering them one
failure at a time.

### L3 — Never hand-edit a projection
**Symptom:** the health check reports the projection hash differs from its
metadata, or references a journal sequence that does not exist.
**Cause:** a projection carries the journal sequence and hash it was built
from. Editing it directly — including stamping a placeholder hash because a
tool was unavailable — breaks that binding.
**Fix:** commit the real event to the journal, then re-project. If the CLI is
unavailable in your context, **hand off**; do not hand-stamp the file. Repair
means committing the true state as a new event, never rewriting history.

### L4 — A red gate may say nothing about your tests
**Symptom:** the gate exits non-zero; you assume the suite is broken.
**Cause:** the gate runs the health check **first** and short-circuits. If the
health check is red, the gate never reaches the tests.
**Fix:** treat them as separate signals. Run your test command directly to
learn test health, and do not report a gate result as a statement about tests.

### L5 — Scope test runs explicitly
**Symptom:** running your test runner from the repository root collects
vendored or template copies of the test tree and dies on import collisions.
**Cause:** duplicate module names in nested payload directories.
**Fix:** pass explicit suite paths, and record the exact command in
`.agentvault/INDEX.md` so every agent runs the same thing.

---

## Adding an entry

Keep the three-part shape (Symptom / Cause / Fix), and link the enforcing file
and line where one exists. The `pre-merge` hook appends takeaways here
automatically; hand-written entries are welcome and should be more detailed.
