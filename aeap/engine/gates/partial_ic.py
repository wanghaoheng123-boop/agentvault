"""G7 — Partial-IC. Does the candidate survive controlling for the reference factors?

Controls are the top-K from U chosen by candidate/control similarity on DEVELOPMENT
FEATURES ONLY — never by looking at the target. Choosing controls by their relationship to
returns would be selecting on the outcome.

Per date: rank the score, the controls and the return; residualize BOTH the ranked score
and the ranked return on the SAME ranked controls plus an intercept; correlate the
residuals. Symmetric residualization is what makes this a partial correlation rather than a
regression coefficient in disguise."""
from __future__ import annotations
import numpy as np
import pandas as pd
from ._common import (Check, PASS, FAIL, INCONCLUSIVE, need, newey_west, ols_resid,
                      spearman)


def pick_controls(ctx, k: int) -> list[str]:
    sims = ctx.artifacts.get("novelty_similarities") or {}
    ranked = sorted(
        (n for n, v in sims.items() if v.get("similarity") is not None),
        key=lambda n: sims[n]["similarity"], reverse=True,
    )
    return ranked[:k]


def run(ctx) -> Check:
    k = int(need(ctx.policy, "G7_partial_ic", "control_k"))
    min_dates = int(need(ctx.policy, "G7_partial_ic", "min_dates"))
    min_abs = float(need(ctx.policy, "G7_partial_ic", "min_abs_mean_partial_ic"))
    min_abs_t = float(need(ctx.policy, "G7_partial_ic", "min_abs_t_stat"))
    min_assets = int(need(ctx.policy, "minimum_assets_per_date"))
    lags = max(0, int(ctx.horizon_days) - 1)

    U = ctx.reference.reference_set()
    names = pick_controls(ctx, k)
    if not names:
        return Check(INCONCLUSIVE, "no usable controls in the reference set",
                     detail={"control_k": k})
    ctx.artifacts["controls"] = names

    cols = {"s": ctx.scores, "r": ctx.returns}
    for n in names:
        cols[f"z::{n}"] = U[n]
    df = pd.DataFrame(cols).dropna()

    per = {}
    for date, g in df.groupby(level="date", observed=True):
        if len(g) < max(min_assets, k + 3):
            continue
        rs = g["s"].rank(method="average").to_numpy()
        rr = g["r"].rank(method="average").to_numpy()
        Z = np.column_stack([g[f"z::{n}"].rank(method="average").to_numpy() for n in names])
        es, er = ols_resid(rs, Z), ols_resid(rr, Z)
        if es is None or er is None:
            continue          # singular design: skipped, never approximated
        c = spearman(es, er)
        if np.isfinite(c):
            per[date] = c
    series = pd.Series(per, dtype="float64").sort_index()
    ctx.artifacts["partial_ic_series"] = series

    detail = {"controls": names, "control_k": k, "hac_lags": lags,
              "dates_with_singular_design": int(len(df.groupby(level='date', observed=True)) - len(series))}
    if len(series) < min_dates:
        return Check(INCONCLUSIVE,
                     f"only {len(series)} dates produced a defined partial-IC, below {min_dates}",
                     n_dates=int(len(series)), threshold=min_dates, detail=detail)

    mean, se, t, n = newey_west(series, lags)
    ctx.artifacts["mean_partial_ic"] = mean
    if not np.isfinite(mean) or not np.isfinite(t):
        return Check(INCONCLUSIVE, "partial-IC mean or HAC standard error is undefined",
                     n_dates=n, units="spearman", detail=detail)
    eff_t = max(min_abs_t, ctx.multiplicity_t_threshold)
    if abs(mean) < min_abs:
        return Check(FAIL, f"|mean partial-IC| {abs(mean):.4f} below floor {min_abs} — spanned by controls",
                     estimate=mean, uncertainty=se, threshold=min_abs, n_dates=n,
                     units="spearman", detail=detail)
    if abs(t) < eff_t:
        return Check(FAIL, f"partial-IC |t| {abs(t):.3f} below {eff_t:.3f}",
                     estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                     units="spearman", detail=detail)
    return Check(PASS, f"mean partial-IC {mean:.4f}, HAC t {t:.3f} over {n} dates",
                 estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                 units="spearman", detail=detail)
