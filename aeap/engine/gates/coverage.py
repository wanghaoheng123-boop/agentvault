"""G3 — Coverage. finite_scores / eligible_assets per date, measured BEFORE label
filtering, plus a separately counted minimum of valid return pairs.

Feature coverage and return-pair availability are distinct: missing labels must never
improve the feature-coverage number, and one surviving asset per date must never
masquerade as broad coverage."""
from __future__ import annotations
import numpy as np
import pandas as pd
from ._common import Check, PASS, FAIL, need


def run(ctx) -> Check:
    min_date_cov = float(need(ctx.policy, "G3_coverage", "min_date_coverage"))
    min_frac = float(need(ctx.policy, "G3_coverage", "min_supported_date_fraction"))
    min_pairs = int(need(ctx.policy, "G3_coverage", "min_valid_return_pairs"))

    eligible = ctx.eligible_counts                       # per date, before label filtering
    finite = ctx.scores.groupby(level="date", observed=True).apply(
        lambda g: int(np.isfinite(g).sum()))
    ratio = (finite / eligible).replace([np.inf, -np.inf], np.nan).dropna()
    if ratio.empty:
        return Check(FAIL, "no scheduled date produced any finite score",
                     threshold=min_date_cov, n_dates=0)
    supported = float((ratio >= min_date_cov).mean())

    pairs = int(pd.DataFrame({"s": ctx.scores, "r": ctx.returns}).dropna().shape[0])
    detail = {"median_date_coverage": float(ratio.median()),
              "supported_date_fraction": supported,
              "min_date_coverage_required": min_date_cov,
              "valid_return_pairs": pairs}
    if supported < min_frac:
        return Check(FAIL, f"only {supported:.3f} of dates meet per-date coverage {min_date_cov}",
                     estimate=supported, threshold=min_frac, n_dates=int(len(ratio)),
                     n_obs=pairs, units="fraction_of_dates", detail=detail)
    if pairs < min_pairs:
        return Check(FAIL, f"{pairs} valid return pairs below minimum {min_pairs}",
                     estimate=supported, threshold=min_frac, n_dates=int(len(ratio)),
                     n_obs=pairs, units="fraction_of_dates", detail=detail)
    return Check(PASS, f"{supported:.3f} of dates meet coverage; {pairs} valid return pairs",
                 estimate=supported, threshold=min_frac, n_dates=int(len(ratio)),
                 n_obs=pairs, units="fraction_of_dates", detail=detail)
