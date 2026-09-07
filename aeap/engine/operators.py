"""Typed factor operators — the only vocabulary a candidate expression may use.

Two scoping rules carry all the temporal safety and must never be relaxed:

  * cross-sectional operators (`cs_*`) group by DATE and act across eligible assets
    within that date. They can never see another date.
  * time-series operators (`ts_*`) group by ASSET, sort by date, and use TRAILING
    windows only. A negative shift or a centered window would read the future, so the
    firewall rejects those before this module is ever reached.

Panels are indexed by (date, asset). Every operator returns a Series on that same index
so expressions compose without an operator ever choosing an alignment of its own.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

MAX_WINDOW = 250
MAX_SCALAR = 1e12
MIN_WINDOW = {"ts_lag": 0, "ts_mean": 1, "ts_std": 2, "ts_delta": 1}


class OperatorError(ValueError):
    pass


def _check_window(w: object, name: str) -> int:
    """Windows must be constant non-negative ints. The firewall enforces this statically;
    this is the runtime backstop for anything constructed programmatically."""
    if isinstance(w, bool) or not isinstance(w, (int, np.integer)):
        raise OperatorError(f"{name}: window must be a constant int, got {type(w).__name__}")
    w = int(w)
    if w < 0:
        raise OperatorError(f"{name}: negative window {w} would read the future")
    if w > MAX_WINDOW:
        raise OperatorError(f"{name}: window {w} exceeds MAX_WINDOW={MAX_WINDOW}")
    return w


def _as_series(x: object, like: pd.Series | None = None) -> pd.Series:
    if isinstance(x, pd.Series):
        if (not isinstance(x.index, pd.MultiIndex) or x.index.names != ["date", "asset"]
                or not x.index.is_unique
                or any(x.index.get_level_values(i).hasnans for i in range(2))):
            raise OperatorError("Series requires unique (date, asset) keys")
        if not pd.api.types.is_numeric_dtype(x.dtype) or pd.api.types.is_bool_dtype(x.dtype):
            raise OperatorError("Series must contain numeric values, not objects or bools")
        return x
    if like is None:
        raise OperatorError("operator requires a Series")
    return pd.Series(_scalar(x), index=like.index, dtype="float64")


def _pair(a: object, b: object) -> tuple[pd.Series, pd.Series]:
    if isinstance(a, pd.Series):
        a, b = _as_series(a), _as_series(b, a)
        if not a.index.equals(b.index):
            raise OperatorError("Series operands must have exactly matching ordered keys")
        return a, b
    if isinstance(b, pd.Series):
        return _as_series(a, b), _as_series(b)
    return _scalar(a), _scalar(b)


def _scalar(x: object) -> float:
    if isinstance(x, (bool, np.bool_)) or not isinstance(x, (int, float, np.integer, np.floating)):
        raise OperatorError("arithmetic requires a numeric scalar or Series")
    value = float(x)
    if not math.isfinite(value):
        raise OperatorError("arithmetic scalar must be finite")
    return value


def _finite_result(result):
    if isinstance(result, pd.Series):
        return result.where(np.isfinite(result))
    return float(result) if np.isfinite(result) else np.nan


# ------------------------------------------------------------------ cross-sectional


def _by_date(s: pd.Series):
    return s.groupby(level="date", group_keys=False, observed=True)


def cs_rank(x: pd.Series) -> pd.Series:
    """Rank within each date, scaled to [0, 1]. Average ties, per gates.v1 rank_tie_policy.

    Ranking is per date across assets. Ranking a pooled panel would leak level
    information across time, which is why there is no pooled variant.
    """
    x = _as_series(x)
    return _by_date(x).apply(lambda g: g.rank(method="average", pct=True))


def cs_demean(x: pd.Series) -> pd.Series:
    x = _as_series(x)
    return _by_date(x).apply(lambda g: g - g.mean())


def cs_zscore(x: pd.Series) -> pd.Series:
    x = _as_series(x)

    def z(g: pd.Series) -> pd.Series:
        sd = g.std(ddof=0)
        # A degenerate cross-section has no information; emit NaN rather than a
        # fabricated zero that would silently count as coverage.
        if not np.isfinite(sd) or sd == 0:
            return pd.Series(np.nan, index=g.index, dtype="float64")
        return (g - g.mean()) / sd

    return _by_date(x).apply(z)


# ---------------------------------------------------------------------- time-series


def _by_asset(s: pd.Series):
    return s.sort_index(level=["asset", "date"]).groupby(level="asset", group_keys=False, observed=True)


def _restore(out: pd.Series, like: pd.Series) -> pd.Series:
    return out.reindex(like.index)


def ts_lag(x: pd.Series, w: int) -> pd.Series:
    """Value from w periods earlier for the same asset. w >= 0 only."""
    w = _check_window(w, "ts_lag")
    x = _as_series(x)
    return _restore(_by_asset(x).shift(w), x)


def ts_mean(x: pd.Series, w: int) -> pd.Series:
    """Trailing mean over w periods INCLUDING the current one."""
    w = _check_window(w, "ts_mean")
    if w < 1:
        raise OperatorError("ts_mean: window must be >= 1")
    x = _as_series(x)
    return _restore(_by_asset(x).rolling(w, min_periods=w).mean().droplevel(0), x)


def ts_std(x: pd.Series, w: int) -> pd.Series:
    w = _check_window(w, "ts_std")
    if w < 2:
        raise OperatorError("ts_std: window must be >= 2")
    x = _as_series(x)
    return _restore(_by_asset(x).rolling(w, min_periods=w).std(ddof=1).droplevel(0), x)


def ts_delta(x: pd.Series, w: int) -> pd.Series:
    """x_t - x_{t-w} for the same asset."""
    w = _check_window(w, "ts_delta")
    if w < 1:
        raise OperatorError("ts_delta: window must be >= 1")
    x = _as_series(x)
    return x - ts_lag(x, w)


# ----------------------------------------------------------------------- arithmetic


def add(a, b):
    a, b = _pair(a, b)
    return _finite_result(a + b)


def sub(a, b):
    a, b = _pair(a, b)
    return _finite_result(a - b)


def mul(a, b):
    a, b = _pair(a, b)
    return _finite_result(a * b)


def div(a, b):
    """Checked division: a zero or non-finite denominator yields NaN, never inf.

    An inf would survive into the cross-section and dominate every rank.
    """
    a, b = _pair(a, b)
    if not isinstance(b, pd.Series):
        return _finite_result(a / b) if b != 0 else np.nan
    denom = b.where(np.isfinite(b) & (b != 0))
    return _finite_result(a / denom)


def neg(a):
    return -_as_series(a) if isinstance(a, pd.Series) else -_scalar(a)


def log1p(a):
    """log(1+x), NaN outside the domain rather than -inf at the boundary."""
    a = _as_series(a)
    values = a.to_numpy(dtype="float64")
    result = np.full(values.shape, np.nan)
    valid = np.isfinite(values) & (values > -1)
    result[valid] = np.log1p(values[valid])
    return pd.Series(result, index=a.index, dtype="float64")


REGISTRY = {
    "cs_rank": cs_rank, "cs_demean": cs_demean, "cs_zscore": cs_zscore,
    "ts_lag": ts_lag, "ts_mean": ts_mean, "ts_std": ts_std, "ts_delta": ts_delta,
    "add": add, "sub": sub, "mul": mul, "div": div, "neg": neg, "log1p": log1p,
}

ARITY = {
    "cs_rank": 1, "cs_demean": 1, "cs_zscore": 1,
    "ts_lag": 2, "ts_mean": 2, "ts_std": 2, "ts_delta": 2,
    "add": 2, "sub": 2, "mul": 2, "div": 2, "neg": 1, "log1p": 1,
}

WINDOW_ARG = {"ts_lag": 1, "ts_mean": 1, "ts_std": 1, "ts_delta": 1}


def result_type(name: str, argument_types: list[str]) -> str:
    """Closed overload table used by the static firewall and bound into its receipt."""
    if name not in REGISTRY or len(argument_types) != ARITY[name]:
        raise OperatorError(f"{name}: unknown signature or wrong arity")
    if name in ("add", "sub", "mul", "div"):
        if any(t not in ("series", "scalar") for t in argument_types):
            raise OperatorError(f"{name}: arithmetic operands must be Series or scalar")
        return "series" if "series" in argument_types else "scalar"
    if name == "neg":
        if argument_types[0] not in ("series", "scalar"):
            raise OperatorError("neg: operand must be Series or scalar")
        return argument_types[0]
    if name in WINDOW_ARG:
        if argument_types != ["series", "window"]:
            raise OperatorError(f"{name}: requires (Series, constant integer window)")
    elif argument_types != ["series"]:
        raise OperatorError(f"{name}: requires a Series argument")
    return "series"


def registry_sha256() -> str:
    """Bind implementation bytes, actual registry callables, contracts and dependencies.

    Hashing vocabulary/arity alone allowed changed implementations to reuse a PASS.
    Source bytes cover helpers as well as public operators. Registry callable source also
    detects runtime substitution of a different function before the execution recheck.
    """
    functions = {}
    for name, func in sorted(REGISTRY.items()):
        try:
            source = inspect.getsource(func)
        except (OSError, TypeError):
            raise OperatorError(f"{name}: implementation source is unavailable")
        functions[name] = {"module": func.__module__, "qualname": func.__qualname__,
                           "source": source}
    spec = {"source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "functions": functions, "arity": ARITY, "window_arg": WINDOW_ARG,
            "min_window": MIN_WINDOW, "max_window": MAX_WINDOW,
            "max_scalar": MAX_SCALAR,
            "dependencies": {"numpy": np.__version__, "pandas": pd.__version__,
                             "python": list(sys.version_info[:3])}}
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
