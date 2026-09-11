"""Causal B1–B5 replay in a fixture-only campaign with retained evidence.

The generator receives a dated, label-free snapshot. Every proposal and feedback exposure
is charged to frozen budgets; all trials survive rejection. Discovery code is frozen before
subsequent scoring. Final labels are opened after close, permanently ending development.
These mechanics do not prove vendor PIT provenance, model vintage, OS isolation, historical
implementability, or production admission. A modern-model replay remains a diagnostic.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from . import evaluate, firewall, panels, sandbox
from .gates._common import per_date_ic, newey_west
from .process_contract import (CampaignLedger, CampaignSpec, MODEL_VINTAGE, canonical, date,
                               digest, identifier, immutable_json, series_payload)
from .reference import Reference


@dataclass(frozen=True)
class GenerationContext:
    decision_date: str
    corpus: tuple[dict, ...]
    library: tuple[dict, ...]
    lessons: tuple[dict, ...]
    prior_trials: tuple[dict, ...]
    remaining_trials: int
    feature_snapshot_sha256: str
    policy_sha256: str
    prompt_sha256: str
    code_sha256: str

    def payload(self) -> dict:
        # A detached JSON value prevents a generator from changing evaluator state.
        return json.loads(canonical(dataclasses.asdict(self)))


class Generator(Protocol):
    """Model/output adapters implement B1 and supply formalizable B2 proposals.

    An adapter must provide a frozen implementation/prompt version and retain its actual
    model output receipts in the proposal. No label or evaluator object is supplied here.
    Model-output replay tests mechanics; prospective trials are needed for new behavior.
    """
    sha256: str

    def generate(self, context: GenerationContext) -> list[dict]: ...


class FrozenGenerator:
    """Replay a frozen proposal plan; never imply these are historical model outputs."""
    def __init__(self, candidates: list[dict]):
        self.candidates = copy.deepcopy(candidates)
        self.sha256 = digest(self.candidates)

    def generate(self, context: GenerationContext) -> list[dict]:
        out = []
        for raw in self.candidates:
            c = {k: copy.deepcopy(v) for k, v in raw.items() if k != "true_signal"}
            if max(date(c.get("known_at", context.decision_date)), date(c.get("valid_from", context.decision_date))) > context.decision_date:
                continue
            c.setdefault("hypothesis", "Frozen fixture proposal: test the preregistered expression")
            c.setdefault("kill_conditions", ["Any required gate is not PASS"])
            c.setdefault("declared_features", ["x", "momentum", "size"])
            c.setdefault("lineage", "candidate")
            c.setdefault("model_output_receipt", {"kind": "FROZEN_PLAN_NOT_HISTORICAL_MODEL",
                                                   "output_sha256": digest(c)})
            out.append(c)
        return out


def _eligible(entries: list[dict], dd: str) -> list[dict]:
    result = []
    for entry in entries:
        # Missing availability is denied, never guessed from a filename or array position.
        if "known_at" not in entry or "valid_from" not in entry:
            raise ValueError("retrieval/library/lesson entry requires known_at and valid_from")
        if date(entry["known_at"]) <= dd and date(entry["valid_from"]) <= dd:
            if entry.get("valid_to") is None or dd < date(entry["valid_to"]):
                if entry.get("source_partition", "development") == "development":
                    result.append(copy.deepcopy(entry))
    return sorted(result, key=lambda x: (str(x.get("id", "")), digest(x)))


def _formalize(raw: dict) -> dict:
    required = {"id", "expression", "expected_sign", "hypothesis", "kill_conditions",
                "declared_features", "model_output_receipt"}
    if not required <= raw.keys():
        raise ValueError(f"incomplete B1/B2 proposal: {sorted(required - raw.keys())}")
    identifier(raw["id"])
    if not isinstance(raw["expression"], str) or not raw["expression"].strip():
        raise ValueError("missing symbolic implementation")
    if type(raw["expected_sign"]) is not int or raw["expected_sign"] not in (-1, 1):
        raise ValueError("proposal sign must be frozen as -1 or +1")
    if not isinstance(raw["hypothesis"], str) or not raw["hypothesis"].strip() or not raw["kill_conditions"]:
        raise ValueError("hypothesis and falsification conditions required")
    if not isinstance(raw["declared_features"], list) or not all(isinstance(n, str) for n in raw["declared_features"]):
        raise ValueError("declared features must be a string list")
    if not isinstance(raw["model_output_receipt"], dict) or not raw["model_output_receipt"].get("output_sha256"):
        raise ValueError("retain actual generator-output receipt")
    out = copy.deepcopy(raw)
    out["expression_sha256"] = hashlib.sha256(raw["expression"].encode()).hexdigest()
    return out


def _metrics(scores: pd.Series | None, returns: pd.Series, schedule: pd.Index, horizon: int) -> dict:
    if scores is None or not len(scores):
        return {"status": "NO_ADMISSIONS", "mean_rank_ic": None, "hac_se": None,
                "n_dates": 0, "n_pairs": 0, "scheduled_dates": len(schedule)}
    ic = per_date_ic(scores, returns, min_assets=3).reindex(schedule)
    mean, se, _, n = newey_west(ic, max(0, horizon - 1), scheduled_dates=schedule)
    finite = lambda x: float(x) if np.isfinite(x) else None
    return {"status": "MEASURED" if n else "INCONCLUSIVE", "mean_rank_ic": finite(mean),
            "hac_se": finite(se), "n_dates": n,
            "n_pairs": int(pd.concat([scores, returns], axis=1).dropna().shape[0]),
            "scheduled_dates": len(schedule)}


def run_replay(*, decision_dates: list, fixture_root: Path, candidates: list[dict] | None = None,
               generator: Generator | None = None, panel: panels.PointInTimePanel | None = None,
               corpus: list[dict] | None = None, initial_lessons: list[dict] | None = None,
               initial_library: list[dict] | None = None, n_dates: int = 260, n_assets: int = 40,
               horizon: int = 5, candidate_budget: int = 6, seed: int = 4242,
               campaign_id: str = "REPLAY", execution_budget: int | None = None,
               generator_budget: int | None = None, feedback_budget: int | None = None,
               final_start: str | None = None, final_end: str | None = None,
               policy_path: Path | None = None, release_final: bool = True) -> dict:
    ds = tuple(date(d) for d in decision_dates)
    if not ds or tuple(sorted(set(ds))) != ds:
        raise ValueError("decision dates must be unique and strictly increasing")
    if type(horizon) is not int or horizon < 1:
        raise ValueError("horizon must be a positive number of scheduled sessions")
    if type(candidate_budget) is not int or candidate_budget < 1:
        raise ValueError("candidate budget must be a positive preregistered integer")
    if generator is not None and candidates is not None:
        raise ValueError("use either a generator adapter or a frozen candidate plan")
    generator = generator or FrozenGenerator(candidates or [])
    fs = date(final_start or (pd.Timestamp(ds[-1]) + pd.offsets.BDay(1)))
    fe = date(final_end or (pd.Timestamp(fs) + pd.offsets.BDay(39)))
    if panel is None:
        start = pd.Timestamp(ds[0]) - pd.offsets.BDay(n_dates - 1)
        total = len(pd.bdate_range(start, fe))
        panel = panels.make_pit_fixture(total, n_assets, seed, 0.35, horizon, start=start)
    if not all(pd.Timestamp(d) in panel.scheduled_dates for d in (*ds, fs, fe)):
        raise ValueError("discovery and final boundaries must lie on the declared schedule")
    corpus, lessons = copy.deepcopy(corpus or []), copy.deepcopy(initial_lessons or [])
    initial_library = copy.deepcopy(initial_library if initial_library is not None else [
        {"id": name, "expression": name, "declared_features": [name], "expected_sign": 1,
         "known_at": date(panel.scheduled_dates[0]), "valid_from": date(panel.scheduled_dates[0])}
        for name in ("momentum", "size")])
    frozen_baseline = _eligible(initial_library, ds[0])
    policy, policy_sha = evaluate.load_gate_policy(policy_path)
    evaluate.assert_executable(policy)
    prompt_sha = digest({"interface": "GenerationContext.v1", "labels": "forbidden"})
    code_sha = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                       (Path(__file__), Path(panels.__file__), Path(evaluate.__file__))})
    # Both arms receive the same maximum execution allowance. The frozen baseline has no
    # search; actual calls, factor count, and search budget are reported, not assumed equal.
    calls = execution_budget if execution_budget is not None else (candidate_budget + len(initial_library)) * (len(ds) + 3)
    spec = CampaignSpec(campaign_id, ds, fs, fe, candidate_budget, calls, calls,
                        generator_budget if generator_budget is not None else len(ds),
                        feedback_budget if feedback_budget is not None else candidate_budget,
                        policy_sha, generator.sha256, digest(corpus), digest(initial_library),
                        prompt_sha, code_sha, data_status=panel.data_status)
    ledger = CampaignLedger(fixture_root, spec)
    if ledger.events():
        raise ValueError("campaign already has trials; use a new fixture root, not an unlogged restart")
    admitted, trials, per_date, comparisons = [], [], [], []

    def execute_factor(factor, feats, dd, arm, purpose):
        kind = "baseline_execution" if arm == "baseline" else "execution"
        ledger.record(kind, {"decision_date": dd, "factor": factor["id"], "purpose": purpose,
                             "expression_sha256": hashlib.sha256(factor["expression"].encode()).hexdigest(),
                             "factor_sha256": digest(factor)})
        fw = firewall.check(factor["expression"], factor["declared_features"], candidate_id=factor["id"])
        if not fw.ok:
            raise ValueError(f"frozen reference code failed firewall: {fw.violations}")
        scores, manifest = sandbox.execute(factor["expression"],
            {k: feats[k] for k in factor["declared_features"]}, candidate_id=factor["id"],
            executor_run_id=f"replay-{arm}-executor", firewall_passed=True)
        if scores is None:
            raise ValueError(f"frozen factor execution failed: {manifest['status']}")
        artifact = ledger.scores(scores)
        manifest_sha = digest(manifest)
        immutable_json(ledger.root / "execution" / f"{manifest_sha}.json", manifest)
        return scores, artifact

    def compare(factors, start, end, asof, *, final=False):
        feats, labels, _, _, schedule = panel.snapshot(asof)
        window = schedule[(schedule >= pd.Timestamp(start)) & (schedule <= pd.Timestamp(end))]
        if not len(window):
            raise ValueError("empty preregistered scoring window")
        selected_labels = labels[labels.index.get_level_values("date").isin(window)]
        arms = {}
        for arm, members in (("process", factors), ("baseline", frozen_baseline)):
            statistics = {}
            for factor in members:
                scores, _ = execute_factor(factor, feats, end, arm, "final" if final else "walk_forward")
                scores = scores[scores.index.get_level_values("date").isin(window)]
                score_ref = ledger.scores(scores)
                statistics[factor["id"]] = {"scores": score_ref,
                    "metrics": _metrics(scores, selected_labels, window, horizon),
                    "frozen_at": factor.get("admitted_at", ds[0]),
                    "expression_sha256": hashlib.sha256(factor["expression"].encode()).hexdigest()}
            arms[arm] = {"factor_count": len(members), "factors": statistics,
                         "status": "MEASURED" if members else "NO_ADMISSIONS"}
        return {"window": {"start": date(start), "end": date(end)}, "asof": date(asof),
                **arms, "label_sha256": digest(series_payload(selected_labels)),
                "uncertainty": {"method": "Bartlett HAC on declared trading grid",
                                "lags": horizon - 1, "scope": "individual-factor IC, not portfolio returns"}}

    for di, dd in enumerate(ds):
        feats, labels, regimes, eligible, schedule = panel.snapshot(dd)
        visible_library = _eligible(initial_library, dd) + [copy.deepcopy(x) for x in admitted if x["admitted_at"] < dd]
        visible_lessons = _eligible(lessons, dd)
        feature_sha = digest({n: series_payload(s) for n, s in sorted(feats.items())})
        context_library = [{k: copy.deepcopy(v) for k, v in x.items() if k != "trial_sha256"} for x in visible_library]
        context_trials = [{k: copy.deepcopy(v) for k, v in x.items() if k in
                           {"decision_date", "candidate", "trial_id", "expression_sha256", "proposal_fingerprint",
                            "all_gates_pass", "checks", "status", "error"}} for x in trials]
        context = GenerationContext(dd, tuple(_eligible(corpus, dd)), tuple(context_library),
                                    tuple(visible_lessons), tuple(context_trials),
                                    candidate_budget - len(trials), feature_sha, policy_sha, prompt_sha, code_sha)
        payload = context.payload()
        context_sha = digest(payload)
        immutable_json(ledger.root / "contexts" / f"{context_sha}.json", payload)
        ledger.record("generation", {"decision_date": dd, "context_sha256": context_sha,
                                      "generator_sha256": generator.sha256})
        proposed = generator.generate(context)
        if not isinstance(proposed, list) or not all(isinstance(c, dict) for c in proposed):
            raise ValueError("generator must return a list of retained proposal objects")
        proposed_sha = digest(proposed)
        immutable_json(ledger.root / "generator_outputs" / f"{proposed_sha}.json", {"proposals": proposed})
        # All controls are reevaluated from frozen code against this same visible panel.
        references = {}
        for entry in visible_library:
            scores, _ = execute_factor(entry, feats, dd, "process", "reference")
            references[entry["id"]] = scores
        ref = Reference(z_asof=references,
                        prior_candidates=[{"candidate_id": t["candidate"], "expression_sha256": t.get("expression_sha256")}
                                          for t in trials], snapshot_id=f"{campaign_id}-asof-{dd}")
        date_results = []
        for raw in proposed:
            if len(trials) >= candidate_budget:
                date_results.append({"candidate": raw.get("id"), "status": "BUDGET_EXHAUSTED"})
                continue
            trial_id = digest({"decision_date": dd, "proposal": raw, "attempt": len(trials)})
            trial = ledger.record("trial", {"decision_date": dd, "proposal": raw, "trial_id": trial_id,
                                  "lineage": raw.get("lineage", "candidate"), "parent_trial": raw.get("parent_trial")})
            result = {"decision_date": dd, "candidate": raw.get("id"), "trial_sha256": trial["sha256"],
                      "all_gates_pass": False, "admissible": False, "trial_id": trial_id}
            try:
                cand = _formalize(raw)
                # Reusing an id with changed semantics hides a mutation; require a new id.
                semantics = digest({k: cand[k] for k in ("expression", "expected_sign", "declared_features")})
                if any(t["candidate"] == cand["id"] and t.get("proposal_fingerprint") != semantics for t in trials):
                    raise ValueError("changed implementation/sign requires a new candidate id and lineage")
                result["proposal_fingerprint"] = semantics
                result["expression_sha256"] = cand["expression_sha256"]
                ledger.record("execution", {"decision_date": dd, "trial_sha256": trial["sha256"], "purpose": "B3_B4"})
                rep = evaluate.evaluate(expression=cand["expression"], declared_features=cand["declared_features"],
                    features=feats, returns=labels, eligible_counts=eligible, scheduled_dates=schedule,
                    regimes=regimes, reference=ref, expected_sign=cand["expected_sign"],
                    candidate_id=cand["id"], campaign_id=campaign_id, horizon_days=horizon,
                    candidate_budget=candidate_budget, author_run_id=f"author-{di}",
                    executor_run_id=f"executor-{di}", judge_run_id=f"judge-{di}", policy_path=policy_path,
                    data_status=panel.data_status, model_vintage_status=MODEL_VINTAGE,
                    score_sink=lambda scores, manifest: ledger.scores(scores))
                report_sha = digest(rep)
                immutable_json(ledger.root / "gate_reports" / f"{report_sha}.json", rep)
                result.update({"all_gates_pass": bool(rep["all_gates_pass"]), "report_sha256": report_sha,
                               "checks": {k: v["status"] for k, v in rep["checks"].items()},
                               "blocking": rep["blocking_reasons"], "score_artifact": rep.get("score_artifact")})
                if rep["all_gates_pass"]:
                    if not rep.get("score_artifact"):
                        raise ValueError("passing candidate lacks its actual score artifact")
                    retained = ledger.read_scores(rep["score_artifact"])
                    if retained.empty or not retained.notna().any():
                        raise ValueError("passing candidate has no usable score values")
                    factor = {"id": f"{cand['id']}-{trial_id[:12]}", "candidate_id": cand["id"],
                              "expression": cand["expression"], "declared_features": cand["declared_features"],
                              "expected_sign": cand["expected_sign"], "known_at": dd, "valid_from": dd,
                              "admitted_at": dd, "score_artifact": rep["score_artifact"],
                              "trial_sha256": trial["sha256"], "trial_id": trial_id}
                    admitted.append(factor)
                    ledger.record("admission", {"fixture_only": True, "factor": factor, "report_sha256": report_sha})
                    immutable_json(ledger.root / "library" / f"{factor['id']}.json", {**factor, "rows": len(retained)})
            except (ValueError, KeyError, evaluate.PolicyNotExecutable) as error:
                result.update({"all_gates_pass": False, "status": "REJECTED", "error": str(error)})
            trials.append(result)
            ledger.record("result", result)
            # Only development gate status/reasons become B5 feedback; never estimates,
            # returns, final scores, or final-test labels. Every exposure costs one token.
            lesson = {"id": trial_id, "known_at": dd, "valid_from": dd,
                      "source_partition": "development", "candidate": raw.get("id"),
                      "outcome": "pass" if result["all_gates_pass"] else "fail",
                      "gate_status": result.get("checks", {}),
                      "blocking_reasons": result.get("blocking", [result.get("error", "")])}
            try:
                ledger.record("feedback", {"decision_date": dd, "trial_sha256": trial["sha256"], "lesson": lesson})
            except ValueError as error:
                if "budget exhausted" not in str(error):
                    raise
                result["feedback_status"] = "BUDGET_EXHAUSTED"
            else:
                lessons.append(lesson)
                ledger.record("lesson", lesson)
            date_results.append(result)
        signature = digest({"context": payload, "proposals": proposed,
                            "trials": [{k: v for k, v in r.items() if k not in {"trial_sha256", "report_sha256", "blocking"}}
                                       for r in date_results]})
        per_date.append({"decision_date": dd, "results": date_results, "context_sha256": context_sha,
                         "decision_fingerprint": signature, "reference_size": len(references),
                         "budget_left": candidate_budget - len(trials),
                         "visible_corpus": payload["corpus"], "visible_lessons": payload["lessons"],
                         "visible_library": payload["library"]})
        if di + 1 < len(ds):
            comparisons.append(compare(copy.deepcopy(admitted), pd.Timestamp(dd) + pd.offsets.BDay(1), ds[di + 1], ds[di + 1]))

    # Reserve final execution cost BEFORE close; afterwards no development action can run.
    # Actual final scoring runs as a separate evaluator phase with frozen factor snapshots.
    frozen_factors = copy.deepcopy(admitted)
    final_origins = panel.labels[(panel.labels.date >= pd.Timestamp(fs)) & (panel.labels.date <= pd.Timestamp(fe))]
    if final_origins.empty:
        raise ValueError("final holdout has no declared outcome horizons")
    final_asof = final_origins.horizon_end.max()
    final_report = None
    if release_final:
        # compare consumes execution budgets and receives labels only here. Close first,
        # then permit final-purpose execution records under the closed campaign contract.
        ledger.close([digest(x) for x in frozen_factors], [digest(x) for x in frozen_baseline])
        final_evidence = compare(frozen_factors, fs, fe, final_asof, final=True)
        final_evidence["attempt_count"] = len(trials)
        final_report = ledger.open_final(final_evidence)
    else:
        ledger.close([digest(x) for x in frozen_factors], [digest(x) for x in frozen_baseline])
    events = ledger.events()
    result = {"schema_version": "1.0", "fixture_root": str(ledger.root), "campaign_id": campaign_id,
              "campaign_sha256": digest(spec.payload()), "is_production": False,
              "can_promote_production_membership": False, "production_admissions": 0,
              "model_vintage_status": MODEL_VINTAGE, "data_status": panel.data_status,
              "readiness": "SYNTHETIC_MECHANICS_ONLY", "decision_dates": list(ds),
              "candidate_budget": candidate_budget, "budget_remaining": candidate_budget - len(trials),
              "per_date": per_date, "trials": trials, "replay_admissions": [x["id"] for x in admitted],
              "walk_forward_comparison": comparisons, "released_final_report": final_report,
              "search_accounting": {kind: sum(e["kind"] == kind for e in events) for kind in
                                    ("generation", "trial", "feedback", "execution", "baseline_execution")},
              "frozen_budgets": spec.payload(), "process_os_isolated": False,
              "note": "Synthetic fixture mechanics only; no production or historical-implementability claim."}
    immutable_json(ledger.root / "replay-result.json", result)
    return result


def causality_check(result: dict, perturbed_result: dict | None = None, *, through=None) -> dict:
    """Validate chronology; causality PASS requires a separately rerun future perturbation.

    A monotone count is not a causality test. The caller changes only future-known context
    or future data, reruns from an empty fixture, and compares earlier decision evidence.
    """
    ds = [date(x["decision_date"]) for x in result["per_date"]]
    if not ds or ds != sorted(set(ds)):
        return {"status": "FAIL", "reason": "decision dates are not strictly increasing"}
    for row in result["per_date"]:
        dd = date(row["decision_date"])
        for entry in row.get("visible_corpus", []) + row.get("visible_lessons", []) + row.get("visible_library", []):
            if date(entry["known_at"]) > dd or date(entry["valid_from"]) > dd:
                return {"status": "FAIL", "reason": "future context is visible before its availability"}
    if perturbed_result is None:
        return {"status": "INCONCLUSIVE", "reason": "chronology valid; independent future-perturbation rerun required"}
    limit = date(through or ds[-1])
    left = {r["decision_date"]: r["decision_fingerprint"] for r in result["per_date"] if r["decision_date"] <= limit}
    right = {r["decision_date"]: r["decision_fingerprint"] for r in perturbed_result["per_date"] if r["decision_date"] <= limit}
    return {"status": "PASS" if left and left == right else "FAIL", "compared_through": limit,
            "compared_decisions": len(left), "reason": "independent earlier decision fingerprints compared"}
