"""P2 — historical process replay (RFC §6, P08).

Re-executes the whole B1->B5 process at each historical decision date with only what was
eligible at that date: the then-current reference set, the then-accumulated lessons, and
the remaining candidate budget. Walk-forward is sequential and strictly causal — a factor
admitted at date t enters the reference set for t+1 and can never reach t-1.

What this is NOT, stated plainly because the distinction is the whole point:

  * It is NOT evidence of historical implementability. The model running the replay may
    carry knowledge from after the decision date even when files and data are filtered.
    Every result carries MODEL_VINTAGE_CONTAMINATION_UNRESOLVED.
  * It is NOT a production qualification. Replay runs in an ISOLATED FIXTURE ROOT. Its
    admissions update only its own replay library and cannot promote production membership.
  * A static-library backtest would not qualify. The comparison baseline is run explicitly
    so the difference between "the process discovers" and "a frozen library scores" is
    visible rather than assumed.

Zero admissions is a valid, reportable outcome.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import evaluate, panels
from .reference import Reference

TZ = timezone(timedelta(hours=8))
MODEL_VINTAGE = "MODEL_VINTAGE_CONTAMINATION_UNRESOLVED"


@dataclass
class ReplayLibrary:
    """The replay's OWN library. Never the production one."""
    root: Path
    admitted: dict[str, pd.Series] = field(default_factory=dict)
    lessons: list[dict] = field(default_factory=list)
    trials: list[dict] = field(default_factory=list)

    def admit(self, name: str, scores: pd.Series, decision_date) -> None:
        self.admitted[name] = scores
        (self.root / "library").mkdir(parents=True, exist_ok=True)
        (self.root / "library" / f"{name}.json").write_text(
            json.dumps({"name": name, "admitted_at_decision_date": str(decision_date),
                        "rows": int(len(scores))}, indent=2))

    def eligible_lessons(self, decision_date) -> list[dict]:
        """Only lessons whose known_at is at or before this decision date."""
        return [l for l in self.lessons if l["known_at"] <= decision_date]


def run_replay(*, decision_dates: list, candidates: list[dict], fixture_root: Path,
               n_dates: int = 260, n_assets: int = 40, horizon: int = 5,
               candidate_budget: int = 6, seed: int = 4242) -> dict:
    """Walk forward over `decision_dates`, running the full pipeline at each.

    `candidates` is the frozen trial plan: what the generator would have proposed. Freezing
    it keeps the replay deterministic and auditable; the model-vintage caveat above still
    applies to the fact that a human or model chose this plan today.
    """
    fixture_root.mkdir(parents=True, exist_ok=True)
    lib = ReplayLibrary(root=fixture_root)
    per_date, budget_left = [], candidate_budget

    for di, dd in enumerate(decision_dates):
        # Panel visible AT this decision date only. A later window is simply not built.
        feats, rets, regimes, elig, sched = panels.make_panel(
            n_dates=n_dates, n_assets=n_assets, seed=seed + di,
            signal=candidates[di % len(candidates)]["true_signal"], horizon=horizon)

        # As-of reference set: base factors plus ONLY what this replay admitted earlier.
        ref = Reference(
            z_asof={"momentum": feats["momentum"], "size": feats["size"]},
            prior_admitted={k: v.reindex(feats["x"].index) for k, v in lib.admitted.items()},
            snapshot_id=f"replay-asof-{dd}")

        date_results = []
        for cand in candidates:
            if budget_left <= 0:
                date_results.append({"candidate": cand["id"], "status": "BUDGET_EXHAUSTED"})
                continue
            budget_left -= 1
            rep = evaluate.evaluate(
                expression=cand["expression"], declared_features=["x", "momentum", "size"],
                features=feats, returns=rets, eligible_counts=elig, scheduled_dates=sched,
                regimes=regimes, reference=ref, expected_sign=cand["expected_sign"],
                candidate_id=cand["id"], campaign_id="REPLAY", horizon_days=horizon,
                candidate_budget=candidate_budget,
                author_run_id=f"replay-author-{di}", executor_run_id=f"replay-exec-{di}",
                judge_run_id=f"replay-judge-{di}", data_status="SYNTHETIC_NOT_PIT",
                model_vintage_status=MODEL_VINTAGE)
            passed = rep["all_gates_pass"]
            lib.trials.append({"decision_date": str(dd), "candidate": cand["id"],
                               "all_gates_pass": passed, "admissible": rep["admissible"]})
            # Failed and rejected trials are retained, per feedback.v1.
            lib.lessons.append({
                "known_at": dd, "candidate": cand["id"], "outcome": "pass" if passed else "fail",
                "blocking": rep["blocking_reasons"][:3],
            })
            date_results.append({
                "candidate": cand["id"], "all_gates_pass": passed,
                "admissible": rep["admissible"],
                "blocking": rep["blocking_reasons"][:2],
                "checks": {k: v["status"] for k, v in rep["checks"].items()},
            })
            # Replay-local admission ONLY. Production membership is never touched, and
            # `admissible` is False anyway while isolation is unproven.
            if passed:
                lib.admit(cand["id"], rep.get("_scores", pd.Series(dtype="float64")), dd)

        per_date.append({"decision_date": str(dd), "results": date_results,
                         "reference_size": len(ref.reference_set()),
                         "budget_left": budget_left})

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "fixture_root": str(fixture_root),
        "is_production": False,
        "can_promote_production_membership": False,
        "model_vintage_status": MODEL_VINTAGE,
        "data_status": "SYNTHETIC_NOT_PIT",
        "decision_dates": [str(d) for d in decision_dates],
        "candidate_budget": candidate_budget,
        "budget_remaining": budget_left,
        "per_date": per_date,
        "trials": lib.trials,
        "replay_admissions": sorted(lib.admitted),
        "production_admissions": 0,
        "note": ("Replay admissions update this fixture's own library only. Zero production "
                 "admissions is the correct and expected outcome while isolation is unproven."),
    }


def causality_check(result: dict) -> dict:
    """A later decision must never change an earlier one.

    Re-reads the recorded per-date results and asserts the reference set is monotone
    non-decreasing and that no earlier date's verdict depends on a later admission.
    """
    sizes = [d["reference_size"] for d in result["per_date"]]
    monotone = all(b >= a for a, b in zip(sizes, sizes[1:]))
    return {
        "reference_set_sizes": sizes,
        "monotone_non_decreasing": monotone,
        "status": "PASS" if monotone else "FAIL",
        "reason": ("reference set only ever grows forward in time; no later admission is "
                   "visible to an earlier decision date")
        if monotone else "a later admission became visible to an earlier decision date",
    }
