# Peer / adversarial reviews

Dialectic gate records live here. **Not** a second task SSOT — completion still
requires tests exit 0 plus lease-backed `avcoord verify` when CURRENT changes.

## Naming

`YYYYMMDD-<topic>-<modelA>-vs-<modelB>.md`

## Required sections

1. **Proposal** — Model A summary + paths touched  
2. **Adversarial audit** — Model B: logic errors, AST/path hallucinations, concurrency  
3. **Verdict** — PASS / FAIL / PASS-WITH-NITS  
4. **Evidence** — commands run and exit codes  

Critical code changes should not be marked COMPLETED in CURRENT without a PASS
(or explicit orchestrator waiver recorded in the review file).
