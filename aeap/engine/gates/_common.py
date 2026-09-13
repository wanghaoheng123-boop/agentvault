"""Shared estimators for the nine gates.

Every statistic here is computed from data by code. No gate may accept a number produced
by a language model, and none of these functions has a "close enough" branch: a degenerate
input returns NaN and the caller turns that into INCONCLUSIVE, never PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

PASS, FAIL, INCONCLUSIVE, NOT_RUN = "PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"


@dataclass
class Check:
    status: str
    reason: str
    estimate: float | None = None
    uncertainty: float | None = None
    threshold: Any = None
    n_dates: int | None = None
    n_obs: int | None = None
    units: str | None = None
    detail: dict | None = field(default=None)

    def to_dict(self) -> dict:
        d = {
            "status": self.status, "reason": self.reason, "estimate": self.estimate,
            "uncertainty": self.uncertainty, "threshold": self.threshold,
            "n_dates": self.n_dates, "n_obs": self.n_obs, "units": self.units,
            "detail": self.detail,
        }
        # JSON cannot carry NaN/inf; a non-finite estimate is reported as null and the
        # status is already INCONCLUSIVE or FAIL by the time we get here.
        for k in ("estimate", "uncertainty"):
            if d[k] is not None and not np.isfinite(d[k]):
                d[k] = None
        return d


class PolicyIncomplete(RuntimeError):
    """A required threshold is null. An incomplete policy is a schema error, never a
    permissive default, so evaluation refuses rather than silently passing."""


def need(policy: dict, *path: str) -> Any:
    node: Any = policy
    for p in path:
        if not isinstance(node, dict) or p not in node:
            raise PolicyIncomplete(f"policy missing {'.'.join(path)}")
        node = node[p]
    if node is None:
        raise PolicyIncomplete(
            f"policy value {'.'.join(path)} is null — calibrate before evaluating"
        )
    return node


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation. NaN when either side is constant — a constant score has
    no cross-sectional information and must not be reported as zero association."""
    if a.size < 3 or b.size != a.size:
        return np.nan
    ra = pd.Series(a).rank(method="average").to_numpy()
    rb = pd.Series(b).rank(method="average").to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    return float(np.corrcoef(ra, rb)[0, 1])


def per_date_ic(scores: pd.Series, returns: pd.Series, min_assets: int, *,
                scheduled_dates: pd.Index | None = None) -> pd.Series:
    """Per-date Spearman IC; unsupported dates retain a NaN slot on the schedule.

    Min-assets filtering removes asset pairs, never time positions. If no schedule is
    supplied, the score's complete date index supplies it; a caller with entirely absent
    dates must supply the original schedule explicitly.
    """
    schedule = (pd.Index(scores.index.get_level_values("date").unique()).sort_values()
                if scheduled_dates is None else pd.Index(scheduled_dates))
    df = pd.DataFrame({"s": scores, "r": returns}).replace([np.inf, -np.inf], np.nan).dropna()
    out: dict[Any, float] = {}
    for date, g in df.groupby(level="date", observed=True):
        if len(g) >= min_assets:
            out[date] = spearman(g["s"].to_numpy(), g["r"].to_numpy())
    return on_schedule(pd.Series(out, dtype="float64").sort_index(), schedule)


def on_schedule(x: pd.Series, scheduled_dates: pd.Index | None = None) -> pd.Series:
    """Place estimates on a supplied trading-period grid, refusing silent compression.

    A supplied schedule is the declared ordered set of trading periods: weekends and
    holidays are handled by its provider, not guessed from calendar-day differences.
    Without it, only a unit-step RangeIndex or an index with explicit pandas frequency
    proves a regular grid. Sparse DatetimeIndex observations alone cannot prove it.
    """
    if not isinstance(x, pd.Series) or isinstance(x.index, pd.MultiIndex):
        raise ValueError("HAC requires one date-indexed Series")
    if not x.index.is_unique or x.index.hasnans or not x.index.is_monotonic_increasing:
        raise ValueError("estimate dates must be unique, nonmissing and increasing")
    if scheduled_dates is None:
        explicit_frequency = getattr(x.index, "freq", None) is not None
        unit_range = isinstance(x.index, pd.RangeIndex) and x.index.step == 1
        if not (explicit_frequency or unit_range):
            raise ValueError("explicit scheduled_dates required for a sparse or unverified time grid")
        schedule = x.index
    else:
        schedule = pd.Index(scheduled_dates)
    if (isinstance(schedule, pd.MultiIndex) or not schedule.is_unique or schedule.hasnans
            or not schedule.is_monotonic_increasing):
        raise ValueError("scheduled_dates must be unique, nonmissing and increasing")
    if not x.index.isin(schedule).all():
        raise ValueError("estimates contain dates outside the declared schedule")
    return x.reindex(schedule).astype("float64")


HAC_CONVENTION = "bartlett_observed_mean_zero_missing_moments_v2"


def newey_west(x: pd.Series, lags: int, *,
               scheduled_dates: pd.Index | None = None) -> tuple[float, float, float, int]:
    """(observed_mean, hac_se, t_stat, n_observed), preserving every scheduled lag.

    Define m_t=1 for a finite estimate and 0 otherwise; the mean solves
    sum m_t (x_t-mu)=0. The sandwich uses moments u_t=m_t(x_t-mu) on the FULL
    trading-period grid and bread 1/n_observed. Missing slots contribute zero moments,
    not imputed returns/ICs. Variance is [sum u_t^2 + 2 sum_j w_j sum_t u_t u_(t-j)]/n^2.
    Bartlett weights are w_j=1-j/(L+1); there is no small-sample correction.

    Thus complete data matches statsmodels cov_hac(use_correction=False). Gaps cannot
    turn nonadjacent dates into lag-1 pairs. Inference concerns the observed-date mean
    under weak dependence of the observed moment process; it does NOT correct selection
    bias from informative missingness or claim an unconditional full-calendar mean.
    Source: statsmodels 0.14.6 sandwich_covariance.S_hac_simple/cov_hac documentation.
    """
    if isinstance(lags, (bool, np.bool_)) or not isinstance(lags, (int, np.integer)) or lags < 0:
        raise ValueError("HAC lags must be a nonnegative integer")
    series = on_schedule(x, scheduled_dates)
    values = series.to_numpy(dtype="float64")
    observed = np.isfinite(values)
    n = int(observed.sum())
    mean = float(values[observed].mean()) if n else np.nan
    if n < 3:
        return mean, np.nan, np.nan, n
    moments = np.zeros(values.size, dtype="float64")
    moments[observed] = values[observed] - mean
    # Keep the declared L in the weights even if the sample has fewer than L+1 periods.
    # Nonexistent longer-lag products are zero; shortening L would change the kernel.
    meat = float(moments @ moments)
    for lag in range(1, min(int(lags), len(moments) - 1) + 1):
        meat += 2 * (1 - lag / (int(lags) + 1)) * float(moments[lag:] @ moments[:-lag])
    if not np.isfinite(meat) or meat <= 0:
        return mean, np.nan, np.nan, n
    se = float(np.sqrt(meat) / n)
    return mean, se, mean / se, n


def date_gaps(estimated: pd.Series, scheduled: pd.Index) -> int:
    return int((~np.isfinite(on_schedule(estimated, scheduled).to_numpy())).sum())


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson of residuals; re-ranking residuals is not partial rank correlation."""
    if a.size < 3 or b.size != a.size or not np.isfinite(a).all() or not np.isfinite(b).all():
        return np.nan
    ca, cb = a - np.mean(a), b - np.mean(b)
    na, nb = float(np.linalg.norm(ca)), float(np.linalg.norm(cb))
    if na == 0 or nb == 0:
        return np.nan
    return float(np.clip((ca / na) @ (cb / nb), -1, 1))


def ols_resid(y: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    """Residuals of y on [1, X]. None when the design is singular or rank-deficient —
    the caller must fail closed rather than fall back to a pseudo-inverse."""
    if (y.size == 0 or X.shape[0] != y.size
            or not np.isfinite(y).all() or not np.isfinite(X).all()):
        return None
    A = np.column_stack([np.ones(y.size), X])
    if A.shape[0] <= A.shape[1]:
        return None
    if np.linalg.matrix_rank(A) < A.shape[1]:
        return None
    try:
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    residual = y - A @ beta
    # A fully spanned ranked variable has zero residual variance. Roundoff from least
    # squares must not manufacture a direction that G7/G9 could accept.
    tolerance = np.finfo(float).eps * max(A.shape) * max(1.0, float(np.linalg.norm(y)))
    if np.linalg.norm(residual) <= tolerance:
        return None
    return residual


def ols_coef(y: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    """Coefficients of y on [1, X]. None on a singular design."""
    if (y.size == 0 or X.shape[0] != y.size
            or not np.isfinite(y).all() or not np.isfinite(X).all()):
        return None
    A = np.column_stack([np.ones(y.size), X])
    if A.shape[0] <= A.shape[1]:
        return None
    if np.linalg.matrix_rank(A) < A.shape[1]:
        return None
    try:
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return beta


def sign_of(x: float | None) -> int:
    """Strict sign. Zero, NaN and None are NOT a direction — G9 treats them as disagreement."""
    if x is None or not np.isfinite(x) or x == 0:
        return 0
    return 1 if x > 0 else -1
