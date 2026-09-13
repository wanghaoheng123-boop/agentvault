"""RFC T14 — nine gates. Deterministic panels with known relationships; no probabilistic
assertions like "any random shuffle must fail"."""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from aeap.engine import evaluate, panels
from aeap.engine.gates import _common as C
from aeap.engine.reference import Reference
from aeap.tests._policy import requires_calibration

FEATS = ["x", "momentum", "size"]


def build(signal=0.3, seed=11, n_dates=300, n_assets=45):
    f, r, g, e, s = panels.make_panel(n_dates=n_dates, n_assets=n_assets, seed=seed,
                                      signal=signal, horizon=5)
    ref = Reference(z_asof={"momentum": f["momentum"], "size": f["size"]},
                    snapshot_id="ref-test")
    return f, r, g, e, s, ref


def ev(expr, signal, expected_sign, **kw):
    f, r, g, e, s, ref = build(signal=signal, **kw)
    return evaluate.evaluate(
        expression=expr, declared_features=FEATS, features=f, returns=r,
        eligible_counts=e, scheduled_dates=s, regimes=g, reference=ref,
        expected_sign=expected_sign, candidate_id="CAND-" + "a" * 12, campaign_id="C",
        horizon_days=5, candidate_budget=20, author_run_id="ra", executor_run_id="re",
        judge_run_id="rj", data_status="SYNTHETIC_NOT_PIT")


def status(rep):
    return {k: v["status"] for k, v in rep["checks"].items()}


# --------------------------------------------------------------------- discrimination

@requires_calibration
def test_true_signal_passes_all_nine():
    rep = ev("cs_rank(ts_mean(x, 3))", 0.35, 1)
    assert rep["all_gates_pass"] is True, status(rep)


@requires_calibration
def test_true_null_fails_the_signal_gates():
    st = status(ev("cs_rank(ts_mean(x, 3))", 0.0, 1))
    assert st["G4_rank_ic"] == "FAIL"
    assert st["G7_partial_ic"] == "FAIL"
    assert st["G8_fama_macbeth"] in ("FAIL", "INCONCLUSIVE")


@requires_calibration
def test_wrong_preregistered_sign_is_a_hard_fail():
    rep = ev("cs_rank(ts_mean(x, 3))", 0.35, -1)
    assert status(rep)["G9_sign_alignment"] == "FAIL"
    assert rep["all_gates_pass"] is False


@requires_calibration
def test_repackaged_input_is_caught_by_g2():
    assert status(ev("x", 0.35, 1))["G2_self_collapse"] == "FAIL"


@requires_calibration
def test_monotone_transform_of_an_input_is_still_collapse():
    assert status(ev("cs_rank(x)", 0.35, 1))["G2_self_collapse"] == "FAIL"


# ----------------------------------------------------------- degenerate never passes

@requires_calibration
def test_constant_score_is_inconclusive_not_zero_association():
    """A constant has no cross-sectional information; reporting IC=0 would be a lie."""
    rep = ev("mul(x, 0)", 0.35, 1)
    st = status(rep)
    assert st["G4_rank_ic"] in ("INCONCLUSIVE", "FAIL")
    assert rep["all_gates_pass"] is False


def test_spearman_of_constant_is_nan():
    assert np.isnan(C.spearman(np.ones(10), np.arange(10.0)))


def test_singular_design_returns_none_not_a_pseudo_inverse():
    y = np.arange(10.0)
    X = np.column_stack([np.ones(10), np.ones(10)])   # collinear with the intercept
    assert C.ols_resid(y, X) is None
    assert C.ols_coef(y, X) is None


def test_sign_of_zero_and_nan_is_not_a_direction():
    assert C.sign_of(0.0) == 0
    assert C.sign_of(float("nan")) == 0
    assert C.sign_of(None) == 0
    assert C.sign_of(-0.5) == -1


def test_newey_west_on_too_few_points_is_undefined():
    _, se, t, n = C.newey_west(pd.Series([1.0, 2.0]), 4)
    assert np.isnan(se) and np.isnan(t) and n == 2


# ----------------------------------------------------------------- policy discipline

def test_uncalibrated_policy_refuses_to_evaluate(tmp_path):
    import yaml
    p = tmp_path / "gates.yaml"
    src = yaml.safe_load(open("aeap/policies/gates.v1.yaml"))
    src["calibration_status"] = "UNCALIBRATED"
    p.write_text(yaml.safe_dump(src))
    pol, _ = evaluate.load_gate_policy(p)
    with pytest.raises(evaluate.PolicyNotExecutable):
        evaluate.assert_executable(pol)


@requires_calibration
def test_null_threshold_raises_policy_incomplete():
    with pytest.raises(C.PolicyIncomplete):
        C.need({"G4_rank_ic": {"min_dates": None}}, "G4_rank_ic", "min_dates")


@requires_calibration
def test_missing_threshold_raises_policy_incomplete():
    with pytest.raises(C.PolicyIncomplete):
        C.need({}, "G4_rank_ic", "min_dates")


# ------------------------------------------------------------------- B4 separation

@pytest.mark.parametrize("judge", ["ra", "re"])
@requires_calibration
def test_judge_run_must_differ_from_author_and_executor(judge):
    f, r, g, e, s, ref = build()
    with pytest.raises(ValueError, match="must differ"):
        evaluate.evaluate(
            expression="cs_rank(x)", declared_features=FEATS, features=f, returns=r,
            eligible_counts=e, scheduled_dates=s, regimes=g, reference=ref,
            expected_sign=1, candidate_id="C", campaign_id="C", horizon_days=5,
            candidate_budget=20, author_run_id="ra", executor_run_id="re",
            judge_run_id=judge)


@requires_calibration
def test_report_binds_every_required_hash():
    rep = ev("cs_rank(ts_mean(x, 3))", 0.35, 1)
    for k in ("code_sha256", "proposal_sha256", "data_sha256", "label_sha256",
              "policy_sha256", "reference_sha256"):
        assert len(rep[k]) == 64, k


@requires_calibration
def test_admission_is_disabled_even_when_all_gates_pass():
    """The activation blocker: containment is not isolation."""
    rep = ev("cs_rank(ts_mean(x, 3))", 0.35, 1)
    assert rep["all_gates_pass"] is True
    assert rep["admissible"] is False
    assert any("isolation" in b for b in rep["blocking_reasons"])


@requires_calibration
def test_model_vintage_status_is_always_carried():
    rep = ev("cs_rank(ts_mean(x, 3))", 0.35, 1)
    assert rep["model_vintage_status"] == "MODEL_VINTAGE_CONTAMINATION_UNRESOLVED"


# ------------------------------------------------------------------------- novelty

@requires_calibration
def test_empty_reference_set_is_inconclusive_not_novel():
    f, r, g, e, s, _ = build()
    rep = evaluate.evaluate(
        expression="cs_rank(ts_mean(x, 3))", declared_features=FEATS, features=f, returns=r,
        eligible_counts=e, scheduled_dates=s, regimes=g,
        reference=Reference(snapshot_id="empty"), expected_sign=1,
        candidate_id="C", campaign_id="C", horizon_days=5, candidate_budget=20,
        author_run_id="ra", executor_run_id="re", judge_run_id="rj")
    assert status(rep)["G6_novelty"] == "INCONCLUSIVE"


@requires_calibration
def test_duplicate_output_is_rejected():
    f, r, g, e, s, ref = build()
    from aeap.engine.gates.duplicate import output_fingerprint
    from aeap.engine import sandbox
    scores, _ = sandbox.execute("cs_rank(ts_mean(x, 3))", {k: f[k] for k in FEATS},
                                candidate_id="prior", executor_run_id="re",
                                firewall_passed=True)
    ref.prior_candidates = [{"candidate_id": "PRIOR",
                             "output_fingerprint": output_fingerprint(scores)}]
    rep = evaluate.evaluate(
        expression="cs_rank(ts_mean(x, 3))", declared_features=FEATS, features=f, returns=r,
        eligible_counts=e, scheduled_dates=s, regimes=g, reference=ref, expected_sign=1,
        candidate_id="C", campaign_id="C", horizon_days=5, candidate_budget=20,
        author_run_id="ra", executor_run_id="re", judge_run_id="rj")
    assert status(rep)["G1_duplicate"] == "FAIL"
