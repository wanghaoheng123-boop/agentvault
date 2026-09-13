"""B4 — independent evaluation. Runs the nine gates over a frozen candidate bundle.

Ordering is a safety property, not a convenience:

    firewall (no data)  ->  sandbox execution (features only, no labels)  ->  gates (labels)

The judge is the first component in the chain that touches labels at all. The generator and
the executor never receive them, so a candidate cannot be tuned against the outcome it is
being measured on.

Admission requires all nine PASS, a PASS firewall receipt, a PASS isolation receipt AND a
peer ACK. This module can report `admissible` but never commits membership — that is the
protected writer's job, and it is disabled workspace-wide.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from importlib.metadata import version

import numpy as np
import pandas as pd
import yaml
from scipy import stats

from . import firewall, sandbox
from .gates import ORDER
from .gates._common import Check, FAIL, INCONCLUSIVE, NOT_RUN, PASS, PolicyIncomplete
from .reference import Reference

TZ = timezone(timedelta(hours=8))
POLICIES = Path(__file__).resolve().parents[1] / "policies"


class PolicyNotExecutable(RuntimeError):
    """Raised when the policy is not calibrated. An uncalibrated policy is a schema error;
    it must never degrade into a permissive default."""


def load_gate_policy(path: Path | None = None) -> tuple[dict, str]:
    p = path or (POLICIES / "gates.v1.yaml")
    raw = p.read_bytes()
    return yaml.safe_load(raw), hashlib.sha256(raw).hexdigest()


def methodology_sha256() -> str:
    """Bind calibration to numerical and execution implementations and dependencies."""
    root = Path(__file__).resolve().parent
    paths = [root / n for n in ("evaluate.py", "panels.py", "reference.py", "operators.py",
                                "firewall.py", "sandbox.py")]
    paths += sorted((root / "gates").glob("*.py"))
    payload = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    payload["dependencies"] = {name: version(name) for name in ("numpy", "pandas", "scipy")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def assert_executable(policy: dict) -> None:
    status = policy.get("calibration_status")
    if status == "UNCALIBRATED":
        raise PolicyNotExecutable(
            "gates.v1 is UNCALIBRATED: every threshold is null. Run "
            "`python3 -m aeap.engine.calibrate` to derive and freeze thresholds before "
            "evaluating. A null threshold is a schema error, not a permissive default."
        )
    if status not in ("CALIBRATED_SYNTHETIC", "CALIBRATED_PIT"):
        raise PolicyNotExecutable(f"unknown calibration_status {status!r}")
    if policy.get("methodology_sha256") != methodology_sha256():
        raise PolicyNotExecutable("calibration methodology hash is absent or stale; recalibrate current implementations")


@dataclass
class EvalContext:
    scores: pd.Series
    returns: pd.Series
    features: dict[str, pd.Series]
    eligible_counts: pd.Series
    scheduled_dates: pd.Index
    regimes: pd.Series | None
    policy: dict
    expected_sign: int
    horizon_days: int
    reference: Reference
    expression_sha256: str
    multiplicity_t_threshold: float = 0.0
    artifacts: dict[str, Any] = field(default_factory=dict)


def multiplicity_threshold(budget: int, alpha: float = 0.05) -> float:
    """Bonferroni over the preregistered campaign budget.

    This controls the family of preregistered trials. It does NOT correct for adaptive
    reuse — feedback changes subsequent hypotheses, which is a separate problem handled by
    the trial ledger and the sealed final test in feedback.v1.
    """
    budget = max(1, int(budget))
    return float(stats.norm.ppf(1.0 - alpha / (2.0 * budget)))


def evaluate(*, expression: str, declared_features: list[str], features: dict[str, pd.Series],
             returns: pd.Series, eligible_counts: pd.Series, scheduled_dates: pd.Index,
             regimes: pd.Series | None, reference: Reference, expected_sign: int,
             candidate_id: str, campaign_id: str, horizon_days: int, candidate_budget: int,
             author_run_id: str, executor_run_id: str, judge_run_id: str,
             policy_path: Path | None = None, data_status: str = "UNKNOWN",
             model_vintage_status: str = "MODEL_VINTAGE_CONTAMINATION_UNRESOLVED",
             score_sink: Callable[[pd.Series, dict], dict] | None = None) -> dict:
    """Run the full pipeline and return a gate-report dict."""
    policy, policy_sha = load_gate_policy(policy_path)
    assert_executable(policy)

    if judge_run_id in {author_run_id, executor_run_id}:
        raise ValueError(
            f"judge run {judge_run_id!r} must differ from the author and executor runs "
            "for this candidate (RFC §4 separation)"
        )

    if len(scheduled_dates) == 0 or scheduled_dates.has_duplicates or not scheduled_dates.is_monotonic_increasing:
        raise ValueError("evaluation requires a nonempty strictly increasing scheduled-date grid")
    if expected_sign not in (-1, 1):
        raise ValueError("expected_sign must be preregistered as -1 or +1")
    if type(candidate_budget) is not int or candidate_budget < 1:
        raise ValueError("candidate_budget must be a positive preregistered integer")
    now = datetime.now(TZ).isoformat(timespec="seconds")
    empty = hashlib.sha256(b"").hexdigest()
    checks: dict[str, Check] = {name: Check(NOT_RUN, "not reached") for name, _ in ORDER}

    def report(blocking: list[str], *, scores=None, fw=None, iso=None) -> dict:
        return {
            "schema_version": "1.0",
            "candidate_id": candidate_id, "campaign_id": campaign_id, "evaluated_at": now,
            "code_sha256": hashlib.sha256(expression.encode("utf-8")).hexdigest(),
            "proposal_sha256": hashlib.sha256(
                json.dumps({"e": expression, "s": expected_sign}, sort_keys=True).encode()).hexdigest(),
            "data_sha256": hashlib.sha256(
                pd.util.hash_pandas_object(pd.concat(features.values())).values.tobytes()).hexdigest(),
            "label_sha256": hashlib.sha256(
                pd.util.hash_pandas_object(returns).values.tobytes()).hexdigest(),
            "policy_sha256": policy_sha,
            "reference_sha256": reference.sha256(),
            "author_run_id": author_run_id, "executor_run_id": executor_run_id,
            "judge_run_id": judge_run_id,
            "development_period": {"start": str(scheduled_dates[0].date()),
                                   "end": str(scheduled_dates[-1].date())},
            "decision_date": str(scheduled_dates[-1].date()),
            "expected_sign": expected_sign,
            "checks": {k: v.to_dict() for k, v in checks.items()},
            "firewall_receipt": fw, "isolation_receipt": iso,
            "admissible": False, "all_gates_pass": False, "blocking_reasons": blocking,
            "evidence_paths": [],
            "data_status": data_status,
            "model_vintage_status": model_vintage_status,
        }

    # --- 1. Firewall. Completes BEFORE any feature handle is supplied.
    fw = firewall.check(expression, declared_features, candidate_id=candidate_id)
    fw_receipt = firewall.receipt_sha256(fw)
    if not fw.ok:
        for name, _ in ORDER:
            checks[name] = Check(NOT_RUN, "firewall rejected the candidate before execution")
        return report([f"firewall: {v}" for v in fw.violations], fw=fw_receipt)

    # --- 2. Sandbox. Features only; `returns` is not in scope here.
    scores, manifest = sandbox.execute(
        expression, {k: features[k] for k in declared_features},
        candidate_id=candidate_id, executor_run_id=executor_run_id, firewall_passed=True)
    iso_receipt = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()
    if scores is None:
        for name, _ in ORDER:
            checks[name] = Check(NOT_RUN, f"execution {manifest['status']}")
        return report([f"execution: {manifest['status']}"], fw=fw_receipt, iso=iso_receipt)

    # Transfer actual scores through an explicit evaluator-owned artifact channel.
    # Reports stay JSON-serializable; replay never falls back to an invented empty Series.
    score_artifact = score_sink(scores.copy(deep=True), manifest) if score_sink else None

    # --- 3. Gates. The judge opens labels only now.
    ctx = EvalContext(
        scores=scores, returns=returns, features=features, eligible_counts=eligible_counts,
        scheduled_dates=scheduled_dates, regimes=regimes, policy=policy,
        expected_sign=expected_sign, horizon_days=horizon_days, reference=reference,
        expression_sha256=fw.expression_sha256,
        multiplicity_t_threshold=multiplicity_threshold(candidate_budget),
    )
    for name, mod in ORDER:
        try:
            checks[name] = mod.run(ctx)
        except PolicyIncomplete as e:
            checks[name] = Check(NOT_RUN, f"policy incomplete: {e}")
        except Exception as e:  # a gate that crashes must never be read as a pass
            checks[name] = Check(INCONCLUSIVE, f"gate raised {type(e).__name__}: {e}")

    blocking = [f"{n}: {c.status} — {c.reason}" for n, c in checks.items() if c.status != PASS]
    rep = report(blocking, fw=fw_receipt, iso=iso_receipt)

    all_pass = all(c.status == PASS for c in checks.values())
    boundary = bool(manifest["isolation"].get("is_security_boundary"))
    if all_pass and not boundary:
        rep["blocking_reasons"].append(
            "isolation: execution was contained but not isolated "
            f"(enforced_by={manifest['isolation']['enforced_by']}); admission stays disabled")
    # `admissible` stays False in this workspace by construction: it additionally requires a
    # real security boundary and a peer ACK, neither of which this module can grant itself.
    rep["admissible"] = False
    if all_pass and boundary:
        rep["blocking_reasons"].append("independent peer ACK and protected admission transaction required")
    if score_artifact is not None:
        rep["score_artifact"] = score_artifact
        rep["evidence_paths"].append(score_artifact["path"])
    rep["all_gates_pass"] = all_pass
    rep["run_manifest"] = manifest
    return rep
