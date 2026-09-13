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
                      pearson, on_schedule, date_gaps, HAC_CONVENTION)


def pick_controls(ctx, k: int) -> list[str]:
    sims = ctx.artifacts.get("novelty_similarities") or {}
    ranked = sorted(
        (n for n, v in sims.items() if v.get("similarity") is not None and np.isfinite(v["similarity"])),
        key=lambda n: (-sims[n]["similarity"], n),
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
    if k < 1 or len(names) != k or any(name not in U for name in names):
        return Check(INCONCLUSIVE, "reference set lacks the complete frozen top-K controls",
                     detail={"control_k": k, "usable_controls": names})
    ctx.artifacts["controls"] = names

    cols = {"s": ctx.scores, "r": ctx.returns}
    for n in names:
        cols[f"z::{n}"] = U[n]
    df = pd.DataFrame(cols).replace([np.inf, -np.inf], np.nan).dropna()

    per, unidentified, insufficient, undefined = {}, 0, 0, 0
    for date, g in df.groupby(level="date", observed=True):
        if len(g) < max(min_assets, k + 3):
            insufficient += 1
            continue
        rs = g["s"].rank(method="average").to_numpy()
        rr = g["r"].rank(method="average").to_numpy()
        Z = np.column_stack([g[f"z::{n}"].rank(method="average").to_numpy() for n in names])
        es, er = ols_resid(rs, Z), ols_resid(rr, Z)
        if es is None or er is None:
            unidentified += 1
            continue          # singular design/fully spanned variable: no estimate
        c = pearson(es, er)
        if np.isfinite(c):
            per[date] = c
        else:
            undefined += 1
    series = on_schedule(pd.Series(per, dtype="float64").sort_index(), ctx.scheduled_dates)
    n_valid = int(series.notna().sum())
    ctx.artifacts["partial_ic_series"] = series

    detail = {"controls": names, "control_k": k, "hac_lags": lags,
              "hac_convention": HAC_CONVENTION, "mean_estimand": "observed_scheduled_dates",
              "residual_correlation": "pearson_of_rank_regression_residuals",
              "dates_with_unidentified_or_spanned_design": unidentified,
              "dates_with_insufficient_assets": insufficient,
              "dates_with_undefined_correlation": undefined,
              "missing_scheduled_dates": date_gaps(series, ctx.scheduled_dates)}
    if n_valid < min_dates:
        return Check(INCONCLUSIVE,
                     f"only {n_valid} dates produced a defined partial-IC, below {min_dates}",
                     n_dates=n_valid, threshold=min_dates, detail=detail)

    mean, se, t, n = newey_west(series, lags, scheduled_dates=ctx.scheduled_dates)
    ctx.artifacts["mean_partial_ic"] = mean
    if not np.isfinite(mean) or not np.isfinite(t):
        return Check(INCONCLUSIVE, "partial-IC mean or HAC standard error is undefined",
                     n_dates=n, units="partial_rank_correlation", detail=detail)
    eff_t = max(min_abs_t, ctx.multiplicity_t_threshold)
    if abs(mean) < min_abs:
        return Check(FAIL, f"|mean partial-IC| {abs(mean):.4f} below floor {min_abs} — spanned by controls",
                     estimate=mean, uncertainty=se, threshold=min_abs, n_dates=n,
                     units="partial_rank_correlation", detail=detail)
    if abs(t) < eff_t:
        return Check(FAIL, f"partial-IC |t| {abs(t):.3f} below {eff_t:.3f}",
                     estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                     units="partial_rank_correlation", detail=detail)
    return Check(PASS, f"mean partial-IC {mean:.4f}, HAC t {t:.3f} over {n} dates",
                 estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                 units="partial_rank_correlation", detail=detail)
