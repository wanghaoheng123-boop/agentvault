"""Derive and freeze gate thresholds.

Two kinds of number get frozen here, and the receipt keeps them distinguishable:

  * EMPIRICAL  — read off the null distribution of the estimator on synthetic panels with
    no true relationship. These answer "how large does this statistic get by chance under
    our own estimator, sample shape and HAC lag choice?" A threshold copied from a paper
    would not answer that.
  * DESIGN     — a declared house convention (coverage floors, control count, similarity
    bounds). These are choices, not measurements, and are labelled as such.

Everything produced here is CALIBRATED_SYNTHETIC. Synthetic panels cannot establish real
market thresholds, so this status can support development and tests but never admission.

    python3 -m aeap.engine.calibrate --replications 200 --freeze
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import panels
from .evaluate import methodology_sha256
from .gates._common import newey_west, per_date_ic

TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "policies" / "gates.v1.yaml"
RECEIPTS = ROOT / "campaigns"


def null_distribution(replications: int, n_dates: int, n_assets: int, horizon: int,
                      min_assets: int) -> dict:
    """|mean IC| and |HAC t| under a true null, using the same estimator the gate uses."""
    abs_ic, abs_t = [], []
    lags = max(0, horizon - 1)
    for r in range(replications):
        feats, rets, _, _, _ = panels.make_panel(
            n_dates=n_dates, n_assets=n_assets, seed=10_000 + r, signal=0.0, horizon=horizon)
        ic = per_date_ic(feats["x"], rets, min_assets)
        # Keep the panel's own trading grid. `.dropna()` compressed unsupported dates out of
        # the calendar, which turned nonadjacent dates into lag-1 pairs and biased the HAC
        # thresholds this file freezes. per_date_ic already returns the series on-schedule.
        mean, se, t, n = newey_west(ic, lags, scheduled_dates=ic.index)
        if np.isfinite(mean):
            abs_ic.append(abs(mean))
        if np.isfinite(t):
            abs_t.append(abs(t))
    a_ic, a_t = np.array(abs_ic), np.array(abs_t)
    return {
        "replications": replications,
        "n_dates": n_dates, "n_assets": n_assets, "horizon_days": horizon, "hac_lags": lags,
        "abs_mean_ic": {"p50": float(np.percentile(a_ic, 50)),
                        "p95": float(np.percentile(a_ic, 95)),
                        "p99": float(np.percentile(a_ic, 99)),
                        "max": float(a_ic.max())},
        "abs_t": {"p50": float(np.percentile(a_t, 50)),
                  "p95": float(np.percentile(a_t, 95)),
                  "p99": float(np.percentile(a_t, 99)),
                  "max": float(a_t.max())},
        "note": ("HAC t under this estimator is NOT standard normal at these sample sizes; "
                 "the empirical p99 is used rather than assuming 2.58."),
    }


def power_check(signal: float, replications: int, n_dates: int, n_assets: int, horizon: int,
                min_assets: int, ic_floor: float, t_floor: float) -> dict:
    """Sanity: a KNOWN relationship of the declared sign must clear the frozen floors.

    A calibration that no true signal can pass is miscalibrated, not conservative.
    """
    hits, signs = 0, []
    lags = max(0, horizon - 1)
    for r in range(replications):
        feats, rets, _, _, _ = panels.make_panel(
            n_dates=n_dates, n_assets=n_assets, seed=90_000 + r, signal=signal, horizon=horizon)
        ic = per_date_ic(feats["x"], rets, min_assets)
        # Keep the panel's own trading grid. `.dropna()` compressed unsupported dates out of
        # the calendar, which turned nonadjacent dates into lag-1 pairs and biased the HAC
        # thresholds this file freezes. per_date_ic already returns the series on-schedule.
        mean, se, t, n = newey_west(ic, lags, scheduled_dates=ic.index)
        if np.isfinite(mean) and np.isfinite(t):
            signs.append(1 if mean > 0 else -1)
            if abs(mean) >= ic_floor and abs(t) >= t_floor:
                hits += 1
    return {"signal": signal, "replications": replications,
            "detection_rate": hits / max(1, replications),
            "sign_consistency": float(np.mean([s == 1 for s in signs])) if signs else None}


def calibrate(replications: int = 200, n_dates: int = 180, n_assets: int = 40,
              horizon: int = 5, min_assets: int = 5) -> dict:
    null = null_distribution(replications, n_dates, n_assets, horizon, min_assets)

    # EMPIRICAL floors: sit just above what the null actually produces.
    ic_floor = round(float(null["abs_mean_ic"]["p99"]), 4)
    t_floor = round(float(max(1.96, null["abs_t"]["p99"])), 3)

    power = power_check(0.25, min(60, replications), n_dates, n_assets, horizon,
                        min_assets, ic_floor, t_floor)

    values = {
        "G2_self_collapse": {"input_rank_similarity_max": 0.95},
        "G3_coverage": {"min_date_coverage": 0.80,
                        "min_supported_date_fraction": 0.90,
                        "min_valid_return_pairs": 500},
        "G4_rank_ic": {"min_dates": 60, "min_abs_mean_ic": ic_floor, "min_abs_t_stat": t_floor},
        "G5_regime_sign": {"min_dates_per_regime": 30},
        "G6_novelty": {"min_overlap_dates": 60, "max_similarity": 0.70},
        "G7_partial_ic": {"control_k": 2, "min_dates": 60,
                          "min_abs_mean_partial_ic": ic_floor, "min_abs_t_stat": t_floor},
        "G8_fama_macbeth": {"min_dates": 60, "min_abs_t_stat": t_floor},
    }
    provenance = {
        "G4_rank_ic.min_abs_mean_ic": "EMPIRICAL — p99 of |mean IC| under the synthetic null",
        "G4_rank_ic.min_abs_t_stat": "EMPIRICAL — max(1.96, p99 of |HAC t| under the null)",
        "G7_partial_ic.min_abs_mean_partial_ic": "EMPIRICAL — same null floor as G4",
        "G7_partial_ic.min_abs_t_stat": "EMPIRICAL — same null floor as G4",
        "G8_fama_macbeth.min_abs_t_stat": "EMPIRICAL — same null floor as G4",
        "G4_rank_ic.min_dates": "DESIGN — a third of a trading year; below this the HAC estimate is unreliable",
        "G7_partial_ic.min_dates": "DESIGN — matches G4",
        "G8_fama_macbeth.min_dates": "DESIGN — matches G4",
        "G5_regime_sign.min_dates_per_regime": "DESIGN — half of min_dates; an unsupported required regime is INCONCLUSIVE",
        "G3_coverage.min_date_coverage": "DESIGN — a factor scoring under 80% of the eligible cross-section is not a broad factor",
        "G3_coverage.min_supported_date_fraction": "DESIGN — tolerates 10% of dates falling short",
        "G3_coverage.min_valid_return_pairs": "DESIGN — counted separately from feature coverage",
        "G2_self_collapse.input_rank_similarity_max": "DESIGN — above 0.95 rank similarity a candidate is a repackaged input",
        "G6_novelty.max_similarity": "DESIGN — 0.70 mean |rank correlation| to any reference factor",
        "G6_novelty.min_overlap_dates": "DESIGN — matches G4 min_dates; thin overlap is INCONCLUSIVE, not novel",
        "G7_partial_ic.control_k": "DESIGN — top 2 controls by development-feature similarity, never by target",
    }
    return {"values": values, "provenance": provenance, "null_distribution": null,
            "power_check": power}


def _substitute(text: str, values: dict, provenance: dict) -> str:
    """Replace `key: null` in place, preserving every comment.

    yaml.safe_dump would round-trip the data correctly and silently discard the rationale
    comments, which are the part a reviewer actually needs. So the YAML text stays the
    source of truth and only the null values are rewritten.
    """
    lines = text.splitlines()
    section = None
    out = []
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*$", line)
        if m:
            section = m.group(1)
        km = re.match(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*null\s*(#.*)?$", line)
        if km and section in values and km.group(2) in values[section]:
            indent, key, comment = km.group(1), km.group(2), km.group(3) or ""
            val = values[section][key]
            tag = provenance.get(f"{section}.{key}", "")
            kind = tag.split(" —")[0] if tag else ""
            note = f"  # {kind}" if kind else (f"  {comment}" if comment else "")
            out.append(f"{indent}{key}: {val}{note}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def freeze(result: dict, policy_path: Path = POLICY) -> dict:
    raw = policy_path.read_text()
    meta = yaml.safe_load(raw)          # metadata only; the TEXT stays the source of truth
    stamp = datetime.now(TZ).isoformat(timespec="seconds")
    receipt = {
        "schema_version": "1.0",
        "calibrated_at": stamp,
        "policy_id": meta["policy_id"],
        "calibration_status": "CALIBRATED_SYNTHETIC",
        "data_basis": "synthetic deterministic panels (aeap/engine/panels.py)",
        "methodology_sha256": methodology_sha256(),
        "methodology_binding": ("hashes evaluate/panels/reference/operators/firewall/sandbox, "
                                "every gate module, and the numpy/pandas/scipy versions. A change "
                                "to any of them invalidates these thresholds."),
        "cannot_support": ["admission", "PIT readiness claims", "empirical performance claims"],
        **result,
    }
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    rpath = RECEIPTS / "calibration-gates-v1.json"
    rpath.write_text(json.dumps(receipt, indent=2) + "\n")

    text = _substitute(raw, result["values"], result["provenance"])
    text = text.replace("calibration_status: UNCALIBRATED",
                        "calibration_status: CALIBRATED_SYNTHETIC")
    text = text.replace("calibration_receipt: null",
                        f"calibration_receipt: {rpath.relative_to(ROOT.parent)}")
    text = text.replace("calibrated_at: null", f"calibrated_at: \"{stamp}\"")
    text = re.sub(r'^calibrated_at: .*$', f'calibrated_at: "{stamp}"', text, count=1, flags=re.M)

    # Thresholds are only meaningful for the code that produced them. Recording the
    # methodology hash is what lets assert_executable reject a policy calibrated against
    # estimator implementations or dependency versions that have since changed.
    meth = methodology_sha256()
    if re.search(r"^methodology_sha256:", text, re.M):
        text = re.sub(r"^methodology_sha256:.*$", f"methodology_sha256: {meth}", text,
                      count=1, flags=re.M)
    else:
        text = re.sub(r'^(calibrated_at: .*)$', rf'\1\nmethodology_sha256: {meth}', text,
                      count=1, flags=re.M)
    policy_path.write_text(text)

    # Round-trip check: the substituted file must still parse and carry no nulls.
    reloaded = yaml.safe_load(policy_path.read_text())
    for section, kv in result["values"].items():
        for k, v in kv.items():
            got = reloaded[section][k]
            if got is None or abs(float(got) - float(v)) > 1e-12:
                raise RuntimeError(f"freeze verification failed for {section}.{k}: {got!r} != {v!r}")
    receipt["frozen_policy_sha256"] = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    rpath.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--replications", type=int, default=200)
    ap.add_argument("--freeze", action="store_true", help="write thresholds into gates.v1.yaml")
    a = ap.parse_args(argv)

    res = calibrate(replications=a.replications)
    print(json.dumps({"null": res["null_distribution"], "power": res["power_check"],
                      "values": res["values"]}, indent=2))
    if a.freeze:
        r = freeze(res)
        print(f"\nfrozen: calibration_status=CALIBRATED_SYNTHETIC receipt={r['calibrated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
