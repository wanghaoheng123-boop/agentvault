"""G8 — Fama-MacBeth. Per date regress the forward excess return on the candidate, the
SAME frozen controls G7 used, and an intercept; then evaluate the mean coefficient series
with HAC standard errors.

A singular or rank-deficient design FAILS CLOSED. Silently dropping a collinear control, or
falling back to a pseudo-inverse, would report a coefficient for a model that was never
identified."""
from __future__ import annotations
import numpy as np
import pandas as pd
from ._common import Check, PASS, FAIL, INCONCLUSIVE, need, newey_west, ols_coef


def run(ctx) -> Check:
    min_dates = int(need(ctx.policy, "G8_fama_macbeth", "min_dates"))
    min_abs_t = float(need(ctx.policy, "G8_fama_macbeth", "min_abs_t_stat"))
    min_assets = int(need(ctx.policy, "minimum_assets_per_date"))
    singular_policy = str(need(ctx.policy, "G8_fama_macbeth", "singular_design"))
    lags = max(0, int(ctx.horizon_days) - 1)

    names = ctx.artifacts.get("controls") or []
    U = ctx.reference.reference_set()
    cols = {"s": ctx.scores, "r": ctx.returns}
    for n in names:
        cols[f"z::{n}"] = U[n]
    df = pd.DataFrame(cols).dropna()

    coefs, singular, scheduled = {}, 0, 0
    for date, g in df.groupby(level="date", observed=True):
        scheduled += 1
        if len(g) < max(min_assets, len(names) + 3):
            continue
        X = np.column_stack(
            [g["s"].to_numpy()] + [g[f"z::{n}"].to_numpy() for n in names]
        )
        beta = ols_coef(g["r"].to_numpy(), X)
        if beta is None:
            singular += 1
            continue
        coefs[date] = float(beta[1])          # beta[0] is the intercept
    series = pd.Series(coefs, dtype="float64").sort_index()
    ctx.artifacts["fm_coef_series"] = series

    detail = {"controls": names, "hac_lags": lags, "singular_dates": singular,
              "scheduled_dates": scheduled, "singular_design_policy": singular_policy}
    if singular and singular_policy.upper() == "FAIL":
        return Check(FAIL, f"{singular} date(s) had a singular design; failing closed",
                     n_dates=int(len(series)), detail=detail)
    if len(series) < min_dates:
        return Check(INCONCLUSIVE,
                     f"only {len(series)} dates produced a coefficient, below {min_dates}",
                     n_dates=int(len(series)), threshold=min_dates, detail=detail)

    mean, se, t, n = newey_west(series, lags)
    ctx.artifacts["mean_fm_coef"] = mean
    if not np.isfinite(mean) or not np.isfinite(t):
        return Check(INCONCLUSIVE, "FM coefficient mean or HAC standard error is undefined",
                     n_dates=n, detail=detail)
    eff_t = max(min_abs_t, ctx.multiplicity_t_threshold)
    if abs(t) < eff_t:
        return Check(FAIL, f"FM |t| {abs(t):.3f} below {eff_t:.3f}",
                     estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                     units="return_per_unit_score", detail=detail)
    return Check(PASS, f"mean FM coefficient {mean:.6g}, HAC t {t:.3f} over {n} dates",
                 estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                 units="return_per_unit_score", detail=detail)
