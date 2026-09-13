"""G4 — Rank-IC. Per-date Spearman of the frozen score against the correctly aligned
forward excess return, aggregated over the declared development period with
dependence-aware uncertainty.

Overlapping horizons induce autocorrelation in the IC series, so the HAC lag is set from
the horizon rather than assumed zero. A constant score yields NaN per date and therefore
INCONCLUSIVE — never a reported zero association."""
from __future__ import annotations
import numpy as np
from ._common import (Check, PASS, FAIL, INCONCLUSIVE, need, per_date_ic, newey_west,
                      date_gaps, HAC_CONVENTION)


def run(ctx) -> Check:
    min_dates = int(need(ctx.policy, "G4_rank_ic", "min_dates"))
    min_abs_ic = float(need(ctx.policy, "G4_rank_ic", "min_abs_mean_ic"))
    min_abs_t = float(need(ctx.policy, "G4_rank_ic", "min_abs_t_stat"))
    min_assets = int(need(ctx.policy, "minimum_assets_per_date"))
    lags = max(0, int(ctx.horizon_days) - 1)

    ic = per_date_ic(ctx.scores, ctx.returns, min_assets, scheduled_dates=ctx.scheduled_dates)
    ctx.artifacts["ic_series"] = ic
    valid = ic.dropna()
    gaps = date_gaps(ic, ctx.scheduled_dates)

    if len(valid) < min_dates:
        return Check(INCONCLUSIVE,
                     f"only {len(valid)} dates produced a defined IC, below min_dates={min_dates}",
                     n_dates=int(len(valid)), threshold=min_dates,
                     detail={"missing_scheduled_dates": gaps})

    mean, se, t, n = newey_west(ic, lags, scheduled_dates=ctx.scheduled_dates)
    ctx.artifacts["mean_ic"] = mean
    detail = {"hac_lags": lags, "hac_convention": HAC_CONVENTION,
              "mean_estimand": "observed_scheduled_dates", "missing_scheduled_dates": gaps,
              "min_abs_mean_ic": min_abs_ic, "min_abs_t_stat": min_abs_t,
              "multiplicity": ctx.policy["G4_rank_ic"].get("multiplicity"),
              "t_threshold_after_multiplicity": ctx.multiplicity_t_threshold}
    if not np.isfinite(mean) or not np.isfinite(t):
        return Check(INCONCLUSIVE, "IC mean or HAC standard error is undefined",
                     estimate=mean if np.isfinite(mean) else None,
                     n_dates=n, units="spearman", detail=detail)
    eff_t = max(min_abs_t, ctx.multiplicity_t_threshold)
    if abs(mean) < min_abs_ic:
        return Check(FAIL, f"|mean IC| {abs(mean):.4f} below economic floor {min_abs_ic}",
                     estimate=mean, uncertainty=se, threshold=min_abs_ic, n_dates=n,
                     units="spearman", detail=detail)
    if abs(t) < eff_t:
        return Check(FAIL, f"|t| {abs(t):.3f} below {eff_t:.3f} after multiplicity",
                     estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                     units="spearman", detail=detail)
    return Check(PASS, f"mean IC {mean:.4f}, HAC t {t:.3f} over {n} dates",
                 estimate=mean, uncertainty=se, threshold=eff_t, n_dates=n,
                 units="spearman", detail=detail)
