"""Deterministic synthetic panels for calibration and tests.

Deliberately NOT probabilistic assertions like "any random shuffle must fail". Every panel
here has a known, constructed relationship so a gate's output can be compared against an
independently derived expectation. Broader null simulations are a separate, preregistered
calibration activity.

Alignment contract, which is the thing that is easy to get silently wrong:
    score  x(t)      is known at date t
    return r(t)      is the excess return realised over (t, t+horizon]
so r(t) is built from noise drawn at t+1 .. t+horizon plus `signal * x(t)`. Overlapping
windows share noise days, which is what makes consecutive returns autocorrelated and the
HAC lag choice meaningful rather than decorative.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _forward_sum(wide: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Sum of the next `horizon` rows, exclusive of the current one: (t, t+horizon]."""
    nxt = wide.shift(-1)
    rev = nxt.iloc[::-1]
    return rev.rolling(horizon, min_periods=horizon).sum().iloc[::-1]


def make_panel(n_dates: int = 180, n_assets: int = 40, seed: int = 0, signal: float = 0.0,
               horizon: int = 5, noise: float = 1.0, regime_split: float = 0.5):
    """Returns (features, returns, regimes, eligible_counts, scheduled_dates).

    `signal` is the true coefficient of x(t) in r(t). signal=0.0 is an exact null.
    """
    rng = np.random.default_rng(seed)
    total = n_dates + horizon + 1
    dates = pd.date_range("2020-01-01", periods=total, freq="B")
    assets = [f"A{i:03d}" for i in range(n_assets)]

    def wide(scale: float = 1.0) -> pd.DataFrame:
        return pd.DataFrame(rng.standard_normal((total, n_assets)) * scale,
                            index=dates, columns=assets)

    x_w, mom_w, size_w = wide(), wide(), wide()
    fwd_noise = _forward_sum(wide(noise), horizon)
    # r(t) = signal * x(t) + realised noise over (t, t+horizon]
    r_w = signal * x_w + fwd_noise

    sched = pd.Index(dates[:n_dates], name="date")

    def stack(w: pd.DataFrame) -> pd.Series:
        s = w.loc[sched].stack()
        s.index.names = ["date", "asset"]
        return s.astype("float64").sort_index()

    features = {"x": stack(x_w), "momentum": stack(mom_w), "size": stack(size_w)}
    returns = stack(r_w)

    cut = sched[int(len(sched) * regime_split)]
    regimes = pd.Series(np.where(sched < cut, "low_vol", "high_vol"), index=sched)
    eligible = pd.Series(float(n_assets), index=sched)
    return features, returns, regimes, eligible, sched


class PointInTimePanel:
    """Versioned fixture tables with an explicit trading-date grid.

    Features have date/asset/feature/value/available_at/revision. ``date`` is the score
    date: a later restatement cannot rewrite a historical score. A late filing must be
    materialized on or after its availability date, with its old observation_time retained.
    Labels additionally carry horizon_end. Universe records carry valid_from/valid_to and
    known_at, so a delisted asset remains in earlier origin-date samples and its final
    outcome is not silently dropped. This validates mechanics, not a vendor's provenance.
    """
    def __init__(self, features: pd.DataFrame, labels: pd.DataFrame, universe: pd.DataFrame,
                 scheduled_dates: pd.DatetimeIndex, regimes: pd.Series | None = None,
                 data_status: str = "SYNTHETIC_NOT_PIT"):
        self.features = features.copy(deep=True)
        self.labels = labels.copy(deep=True)
        self.universe = universe.copy(deep=True)
        self.scheduled_dates = pd.DatetimeIndex(scheduled_dates, name="date")
        if (self.scheduled_dates.has_duplicates or not self.scheduled_dates.is_monotonic_increasing
                or self.scheduled_dates.hasnans or self.scheduled_dates.tz is not None):
            raise ValueError("schedule must be a strictly increasing, finite, timezone-naive grid")
        if not len(self.scheduled_dates):
            raise ValueError("empty schedule")
        for table, cols in ((self.features, {"date", "asset", "feature", "value", "available_at", "revision"}),
                            (self.labels, {"date", "asset", "value", "horizon_end", "available_at", "revision"}),
                            (self.universe, {"asset", "valid_from", "valid_to", "known_at", "revision"})):
            if not cols <= set(table):
                raise ValueError(f"missing PIT columns: {cols - set(table)}")
            for col in cols & {"date", "available_at", "horizon_end", "valid_from", "valid_to", "known_at"}:
                table[col] = pd.to_datetime(table[col])
                if col != "valid_to" and table[col].isna().any():
                    raise ValueError(f"missing {col}")
                if table[col].dt.tz is not None:
                    raise ValueError("PIT timestamps must use the schedule timezone")
        if not self.features["date"].isin(self.scheduled_dates).all() or not self.labels["date"].isin(self.scheduled_dates).all():
            raise ValueError("observations outside declared trading grid")
        if (self.labels.horizon_end <= self.labels.date).any():
            raise ValueError("label horizon must finish after its origin")
        if (self.labels.available_at < self.labels.horizon_end).any():
            raise ValueError("label cannot be available before its complete horizon matures")
        for table, keys in ((self.features, ["date", "asset", "feature", "available_at", "revision"]),
                            (self.labels, ["date", "asset", "available_at", "revision"]),
                            (self.universe, ["asset", "valid_from", "known_at", "revision"])):
            if table.duplicated(keys).any():
                raise ValueError("ambiguous duplicate vintage")
        if (self.universe.valid_to.notna() & (self.universe.valid_to <= self.universe.valid_from)).any():
            raise ValueError("universe interval is empty or reversed")
        self.regimes = regimes.copy() if regimes is not None else None
        self.data_status = data_status

    def snapshot(self, decision_date, *, start=None, end=None):
        """Return features known at their score dates and labels matured by decision_date.

        The declared grid is retained even if an estimate or asset observation is missing.
        Label rows are independently masked; row position never determines maturity.
        """
        dd = pd.Timestamp(decision_date)
        sched = self.scheduled_dates[self.scheduled_dates <= dd]
        if start is not None:
            sched = sched[sched >= pd.Timestamp(start)]
        if end is not None:
            sched = sched[sched <= pd.Timestamp(end)]
        if not len(sched):
            raise ValueError("no scheduled observations visible at decision date")
        membership = []
        for day in sched:
            u = self.universe[(self.universe.known_at <= day) & (self.universe.valid_from <= day)]
            u = u.sort_values(["known_at", "revision"]).drop_duplicates("asset", keep="last")
            u = u[u.valid_to.isna() | (u.valid_to > day)]
            membership.extend((day, str(a)) for a in u.asset)
        idx = pd.MultiIndex.from_tuples(sorted(membership), names=["date", "asset"])
        eligible = pd.Series([sum(d == day for d, _ in membership) for day in sched],
                             index=sched, dtype="float64")
        f = self.features[self.features.date.isin(sched) & (self.features.available_at <= self.features.date)
                          & (self.features.available_at <= dd)]
        f = f.sort_values(["available_at", "revision"]).drop_duplicates(["date", "asset", "feature"], keep="last")
        features = {}
        for name in sorted(self.features.feature.unique()):
            rows = f[f.feature == name].set_index(["date", "asset"])
            features[name] = rows.value.reindex(idx).astype("float64").rename(name)
        r = self.labels[self.labels.date.isin(sched) & (self.labels.available_at <= dd)
                        & (self.labels.horizon_end <= dd)]
        r = r.sort_values(["available_at", "revision"]).drop_duplicates(["date", "asset"], keep="last")
        returns = r.set_index(["date", "asset"]).value.reindex(idx).astype("float64").rename("fwd_excess")
        regimes = self.regimes.reindex(sched) if self.regimes is not None else None
        return features, returns, regimes, eligible, sched


def make_pit_fixture(n_dates=260, n_assets=40, seed=4242, signal=0.35, horizon=5):
    """Single frozen synthetic world for replay; a candidate never changes its true signal."""
    features, returns, regimes, _, sched = make_panel(n_dates, n_assets, seed, signal, horizon)
    expanded = pd.bdate_range(sched[0], periods=n_dates + horizon + 1)
    rows = []
    for name, values in features.items():
        for (day, asset), value in values.items():
            rows.append({"date": day, "asset": asset, "feature": name, "value": value,
                         "observation_time": day, "available_at": day, "revision": 0})
    label_rows = []
    maturity = {d: expanded[i + horizon] for i, d in enumerate(sched)}
    for (day, asset), value in returns.items():
        label_rows.append({"date": day, "asset": asset, "value": value,
                           "horizon_end": maturity[day], "available_at": maturity[day], "revision": 0})
    universe = pd.DataFrame([{"asset": a, "valid_from": sched[0], "valid_to": pd.NaT,
                              "known_at": sched[0], "revision": 0}
                             for a in features["x"].index.get_level_values("asset").unique()])
    return PointInTimePanel(pd.DataFrame(rows), pd.DataFrame(label_rows), universe, sched, regimes)
