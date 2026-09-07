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


def per_date_ic(scores: pd.Series, returns: pd.Series, min_assets: int) -> pd.Series:
    """Datewise Spearman IC between score at t and the forward return aligned to t.

    Per date across eligible assets — never pooled. Dates with fewer than `min_assets`
    valid pairs are DROPPED, not imputed, so a thin date cannot masquerade as evidence.
    """
    df = pd.DataFrame({"s": scores, "r": returns}).dropna()
    out: dict[Any, float] = {}
    for date, g in df.groupby(level="date", observed=True):
        if len(g) < min_assets:
            continue
        out[date] = spearman(g["s"].to_numpy(), g["r"].to_numpy())
    return pd.Series(out, dtype="float64").sort_index()


def newey_west(x: pd.Series, lags: int) -> tuple[float, float, float, int]:
    """(mean, hac_se, t_stat, n) for the mean of a time series.

    Bartlett-kernel HAC. The series must already be the ordered sequence of dates that
    actually produced an estimate; callers report how many scheduled dates are missing so
    a reader can see that gaps were not silently compressed into adjacent observations.
    """
    v = x.dropna().to_numpy(dtype="float64")
    n = v.size
    if n < 3:
        return (float(np.mean(v)) if n else np.nan), np.nan, np.nan, n
    mu = float(np.mean(v))
    e = v - mu
    lags = max(0, min(int(lags), n - 1))
    s = float(np.dot(e, e) / n)
    for j in range(1, lags + 1):
        cov = float(np.dot(e[j:], e[:-j]) / n)
        s += 2.0 * (1.0 - j / (lags + 1.0)) * cov
    if not np.isfinite(s) or s <= 0:
        return mu, np.nan, np.nan, n
    se = float(np.sqrt(s / n))
    return mu, se, (mu / se if se > 0 else np.nan), n


def date_gaps(estimated: pd.Series, scheduled: pd.Index) -> int:
    return int(len(scheduled) - len(estimated.dropna()))


def ols_resid(y: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    """Residuals of y on [1, X]. None when the design is singular or rank-deficient —
    the caller must fail closed rather than fall back to a pseudo-inverse."""
    if y.size == 0 or X.shape[0] != y.size:
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
    return y - A @ beta


def ols_coef(y: np.ndarray, X: np.ndarray) -> np.ndarray | None:
    """Coefficients of y on [1, X]. None on a singular design."""
    if y.size == 0 or X.shape[0] != y.size:
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
