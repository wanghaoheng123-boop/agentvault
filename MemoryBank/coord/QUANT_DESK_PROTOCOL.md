---
version: 1.0
last_updated: 2026-09-05T14:30:19+08:00
type: quant_desk_protocol
---

# Quant Desk Protocol (P2)

Activate `@Research-Agent` + playbook `quant_researcher.md` (and mathematician when proofs/derivations).

## Loop (mandatory)
1. **Question** — falsifiable hypothesis / decision ask
2. **Data inventory** — sources, as-of, coverage gaps (UNCERTAIN listed)
3. **Method** — model/estimator; state assumptions
4. **Compute** — prefer code/tools; log parameters in events.jsonl
5. **Falsify** — seek disconfirming evidence; stress scenarios
6. **Label** — FACT vs ESTIMATE vs SCENARIO; no false precision
7. **Peer** — `@Peer-Reviewer` on methodology before publishable COMPLETE

## Forbidden
- Fabricated tickers, backtests, or citations
- Mixing units (e.g. bp vs %) without conversion note
- Presenting point forecasts as certainty
- Skipping data inventory because “model knows”

## Artifacts
- Data inventory note under VectorRAG or session refs
- Parameter log → EpisodicTracker/events.jsonl
- Publishable draft only after H1–H7 gate
