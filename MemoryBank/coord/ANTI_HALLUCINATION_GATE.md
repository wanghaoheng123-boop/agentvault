---
version: 1.0
last_updated: 2026-09-05T14:30:19+08:00
type: anti_hallucination_gate
---

# Anti-Hallucination Gate (P2)

Not a second SSOT. Run before marking research/quant/docs COMPLETE.

| ID | Check | Pass criterion |
|----|-------|----------------|
| H1 | Tool-first facts | Claims about code/APIs/files verified by read/run, not memory |
| H2 | Citation realness | Every citation has DOI/URL/path; use citation-verification-gate when bibliography present |
| H3 | Numeric provenance | Each number has source + as-of + unit |
| H4 | Epistemic labels | FACT/ESTIMATE/OPINION/SCENARIO/UNCERTAIN applied |
| H5 | No silent gaps | Missing data → UNCERTAIN or explicit null, not filled fiction |
| H6 | Cross-check | Second agent or adversarial pass on high-stakes claims |
| H7 | Reproducibility | Quant/math steps reproducible from logged parameters |

**Block COMPLETE** if any of H2–H5 fail on claim-heavy deliverables.

Structural mitigations already in AgentVault: GraphRAG over fuzzy dump; lease hooks; peer_reviewer mail; events.jsonl parameter logs.

## Quant epic extension (VERIFY_QUANT)

When ETS tags include `quant` / `numeric` / `backtest` / publishable market claims, also require:

| ID | Check | Pass criterion |
|----|-------|----------------|
| Q-A | Provenance | Decision numbers have source / as-of / unit / revision |
| Q-B | Status labels | Claims tagged FACT/ESTIMATE/OPINION/SCENARIO |
| Q-C | Citations | Refs resolve (DOI/URL/path); unresolvable → FAIL or demote |
| Q-D | Falsification | Pre-registered kill tests executed & logged |
| Q-E | Leakage | No lookahead / train-test contamination / omitted costs |
| Q-F | Peer ACK | `peer_reviewer` adversarial note + ack |
| Q-G | Units | % vs bps vs FX/scale consistency |

Quant COMPLETE only when H1–H7 and Q-A–Q-G PASS. See `QUANT_DESK_PROTOCOL.md`.
