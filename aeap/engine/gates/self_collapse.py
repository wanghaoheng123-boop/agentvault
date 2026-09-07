"""G2 — Self-collapse. Reject an expression that is just a repackaging of one of its own
declared inputs.

Exact algebraic equivalence is checked on the nonzero/non-null domain only: x*y/x is NOT
equivalent to x where y is zero or null, so cancelling across those would be wrong.
Approximate collapse uses the same absolute-before-time-average convention as G6."""
from __future__ import annotations
import numpy as np
import pandas as pd
from ._common import Check, PASS, FAIL, INCONCLUSIVE, need, spearman


def run(ctx) -> Check:
    bound = float(need(ctx.policy, "G2_self_collapse", "input_rank_similarity_max"))
    s = ctx.scores
    worst_name, worst = None, -1.0
    for name, feat in ctx.features.items():
        df = pd.DataFrame({"s": s, "f": feat}).dropna()
        if df.empty:
            continue
        # exact equivalence on the shared valid domain
        if np.allclose(df["s"].to_numpy(), df["f"].to_numpy(), rtol=1e-12, atol=1e-12):
            return Check(FAIL, f"expression is numerically identical to declared input '{name}'",
                         estimate=1.0, threshold=bound, detail={"input": name, "kind": "exact"})
        per_date = []
        for _, g in df.groupby(level="date", observed=True):
            c = spearman(g["s"].to_numpy(), g["f"].to_numpy())
            if np.isfinite(c):
                per_date.append(abs(c))
        if not per_date:
            continue
        sim = float(np.mean(per_date))
        if sim > worst:
            worst_name, worst = name, sim
    if worst < 0:
        return Check(INCONCLUSIVE, "no overlapping valid observations with any declared input",
                     threshold=bound)
    if worst >= bound:
        return Check(FAIL, f"rank similarity {worst:.4f} to input '{worst_name}' at or above bound",
                     estimate=worst, threshold=bound, detail={"input": worst_name})
    return Check(PASS, f"max input rank similarity {worst:.4f} below bound",
                 estimate=worst, threshold=bound, detail={"closest_input": worst_name})
