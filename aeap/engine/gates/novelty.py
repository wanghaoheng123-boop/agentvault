"""G6 — Novelty vs the reference set. U = Z_asof UNION prior_admitted, excluding this
candidate.

Similarity is max over z of the time-average of |per-date Spearman(score, z)|. Absolute
value BEFORE the time average, so a factor that flips sign across dates cannot average its
way to apparent independence, and an inverted known factor is caught.

An empty or thin reference set is INCONCLUSIVE. Having nothing to compare against is not
evidence of novelty."""
from __future__ import annotations
import numpy as np
import pandas as pd
from ._common import Check, PASS, FAIL, INCONCLUSIVE, need, spearman


def similarity(scores, z) -> tuple[float, int]:
    df = pd.DataFrame({"s": scores, "z": z}).dropna()
    per = []
    for _, g in df.groupby(level="date", observed=True):
        c = spearman(g["s"].to_numpy(), g["z"].to_numpy())
        if np.isfinite(c):
            per.append(abs(c))
    return (float(np.mean(per)) if per else np.nan), len(per)


def run(ctx) -> Check:
    bound = float(need(ctx.policy, "G6_novelty", "max_similarity"))
    min_overlap = int(need(ctx.policy, "G6_novelty", "min_overlap_dates"))
    U = ctx.reference.reference_set()
    if not U:
        return Check(INCONCLUSIVE, "reference set U is empty; novelty cannot be established",
                     threshold=bound)

    sims, supported = {}, 0
    for name, z in U.items():
        sim, n = similarity(ctx.scores, z)
        sims[name] = {"similarity": None if not np.isfinite(sim) else sim, "overlap_dates": n}
        if n >= min_overlap and np.isfinite(sim):
            supported += 1
    ctx.artifacts["novelty_similarities"] = sims
    usable = {k: v["similarity"] for k, v in sims.items()
              if v["overlap_dates"] >= min_overlap and v["similarity"] is not None}
    detail = {"per_reference": sims, "min_overlap_dates": min_overlap, "reference_size": len(U)}
    if not usable:
        return Check(INCONCLUSIVE,
                     f"no reference factor has {min_overlap} overlapping dates", threshold=bound,
                     detail=detail)
    worst_name = max(usable, key=lambda k: usable[k])
    worst = usable[worst_name]
    if worst >= bound:
        return Check(FAIL, f"similarity {worst:.4f} to '{worst_name}' at or above bound {bound}",
                     estimate=worst, threshold=bound, detail=detail)
    return Check(PASS, f"max similarity {worst:.4f} (to '{worst_name}') below bound {bound}",
                 estimate=worst, threshold=bound,
                 detail={**detail, "supported_references": supported})
