"""DEV-ONLY yfinance dataset adapter.

This exists so the pipeline can be exercised on data that is shaped like a real market
panel. It CANNOT satisfy the RFC's P1 bitemporal contract, and every manifest it writes
says so in machine-readable form:

    pit_compliant:          false
    eligible_for_admission: false
    biases:                 [survivorship, adjusted_prices, no_pit_fundamentals,
                             no_delistings, no_revision_vintages]

Why it cannot be PIT, concretely:
  * The universe is whatever tickers you pass today, so names that were delisted or removed
    from an index are absent. Every backtest over it is survivorship-biased upward.
  * Adjusted closes are restated backwards after each split and dividend, so a price
    "as of" 2021 in today's download is not the price a 2021 decision-maker saw.
  * There are no filing dates, no restatement vintages and no as-of fundamentals, so
    `available_at` cannot be derived — only assumed.

A snapshot from here is fine for smoke-testing the pipeline's shape. Any gate report built
on it carries data_status=DEV_ONLY_NOT_PIT and can never reach admission.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "datasets" / "manifests"
SNAPSHOTS = ROOT / "datasets" / "snapshots"

BIASES = [
    "survivorship",
    "adjusted_prices",
    "no_pit_fundamentals",
    "no_delistings",
    "no_revision_vintages",
]


def fetch(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Download adjusted closes. Network call — the only one in the engine."""
    import yfinance as yf  # imported lazily so the engine has no hard network dependency

    raw = yf.download(tickers, start=start, end=end, auto_adjust=True,
                      progress=False, group_by="column")
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    return close.dropna(how="all").sort_index()


def build_panel(close: pd.DataFrame, horizon: int = 5) -> dict:
    """Shape a close-price frame into the engine's panel contract.

    The forward return is excess of the equal-weighted universe mean over the same window,
    matching `return_definition` in gates.v1. Returns are computed from prices that were
    themselves restated, which is exactly why this is not PIT.
    """
    close = close.sort_index()
    ret1 = close.pct_change()
    fwd = close.shift(-horizon) / close - 1.0
    fwd_excess = fwd.sub(fwd.mean(axis=1), axis=0)

    mom = close.pct_change(21)
    vol = ret1.rolling(21, min_periods=21).std()

    dates = close.index[: len(close.index) - horizon]

    def stack(w: pd.DataFrame, name: str) -> pd.Series:
        s = w.loc[dates].stack()
        s.index.names = ["date", "asset"]
        return s.astype("float64").sort_index().rename(name)

    features = {"momentum_21d": stack(mom, "momentum_21d"),
                "volatility_21d": stack(vol, "volatility_21d")}
    returns = stack(fwd_excess, "fwd_excess")
    eligible = pd.Series(float(close.shape[1]), index=pd.Index(dates, name="date"))

    v = vol.loc[dates]
    med = v.median(axis=1)
    regimes = pd.Series(np.where(med > med.median(), "high_vol", "low_vol"),
                        index=pd.Index(dates, name="date"))
    return {"features": features, "returns": returns, "regimes": regimes,
            "eligible_counts": eligible, "scheduled_dates": pd.Index(dates, name="date")}


def write_snapshot(panel: dict, tickers: list[str], start: str, end: str,
                   horizon: int = 5) -> dict:
    """Persist the panel and a provenance manifest that bars it from admission."""
    payload = pd.concat(
        [*panel["features"].values(), panel["returns"]], axis=1
    ).sort_index()
    body = payload.to_csv().encode("utf-8")
    content_sha = hashlib.sha256(body).hexdigest()

    snap_dir = SNAPSHOTS / content_sha[:16]
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "panel.csv").write_bytes(body)

    manifest = {
        "schema_version": "1.0",
        "snapshot_id": f"dev-yf-{content_sha[:16]}",
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": "yfinance (adjusted closes)",
        "license": "Yahoo Finance terms apply; not redistributable",
        "pit_compliant": False,
        "biases": BIASES,
        "eligible_for_admission": False,
        "universe": f"caller-supplied list of {len(tickers)} tickers: {','.join(sorted(tickers))}",
        "frequency": "daily",
        "columns": [
            {"name": "momentum_21d", "kind": "feature", "units": "fraction",
             "available_at_rule": "ASSUMED same-day close; no filing or vintage data exists",
             "revision_policy": "prices are restated retroactively after splits/dividends"},
            {"name": "volatility_21d", "kind": "feature", "units": "fraction_stdev",
             "available_at_rule": "ASSUMED same-day close; no filing or vintage data exists",
             "revision_policy": "prices are restated retroactively after splits/dividends"},
            {"name": "fwd_excess", "kind": "label",
             "units": f"fraction over {horizon} sessions, excess of equal-weighted mean",
             "available_at_rule": f"matures {horizon} sessions after the decision date",
             "revision_policy": None},
        ],
        "row_count": int(payload.shape[0]),
        "content_sha256": content_sha,
        "date_range": {"start": str(start), "end": str(end)},
        "warning": ("DEV ONLY. Survivorship-biased and retroactively adjusted. Cannot satisfy "
                    "the P1 bitemporal contract and is barred from any admission path."),
    }
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    mpath = MANIFESTS / f"{manifest['snapshot_id']}.json"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest["_manifest_path"] = str(mpath)
    manifest["_panel_path"] = str(snap_dir / "panel.csv")
    return manifest


def data_status(manifest: dict) -> str:
    return "DEV_ONLY_NOT_PIT" if not manifest.get("pit_compliant") else "PIT"
