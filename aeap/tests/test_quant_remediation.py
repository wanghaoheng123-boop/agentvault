"""Independent deterministic regressions for audit C03/C06 and RFC T12/T14.

References (checked 2026-09-08):
* Python AST docs: https://docs.python.org/3/library/ast.html (parser stack exhaustion).
* statsmodels 0.14.6 cov_hac: https://www.statsmodels.org/v0.14.6/generated/
  statsmodels.stats.sandwich_covariance.cov_hac.html (equally spaced HAC).

No calibration, active-state writes, random panels or assertion on random significance.
Numeric gate fixtures declare their own permissive TEST thresholds, never production
thresholds. Statsmodels is an independent implementation, not used by the tested kernel.
"""
from __future__ import annotations

from itertools import product
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from scipy.stats import rankdata
from statsmodels.regression.linear_model import OLS
from statsmodels.stats.sandwich_covariance import cov_hac

from aeap.engine import firewall, operators
from aeap.engine.gates import _common as C
from aeap.engine.gates import rank_ic, partial_ic, fama_macbeth, sign_alignment
from aeap.engine.reference import Reference


@pytest.mark.parametrize("expression", [
    "-" * 900 + "x",
    "x+" * 400 + "x",
    "cs_rank(" * 40 + "x" + ")" * 40,
    "x * " + "9" * 100,
    "x+" * 1100 + "x",
])
def test_parser_budget_rejects_before_python_ast(monkeypatch, expression):
    def forbidden_parse(*args, **kwargs):
        pytest.fail("unbounded source reached Python AST parser")
    monkeypatch.setattr(firewall.ast, "parse", forbidden_parse)
    result = firewall.check(expression, ["x"])
    assert not result.ok
    assert any("budget" in v or "depth" in v or "max_source_bytes" in v
               for v in result.violations)


@pytest.mark.parametrize("expression", [
    'f"{' + '-' * 900 + 'x}"',
    'rf"{' + '(' * 100 + 'x' + ')' * 100 + '}"',
])
def test_interpolated_strings_rejected_before_hidden_expression_parse(monkeypatch, expression):
    def forbidden_parse(*args, **kwargs):
        pytest.fail("interpolated source reached the AST parser")
    monkeypatch.setattr(firewall.ast, "parse", forbidden_parse)
    result = firewall.check(expression, ["x"])
    assert not result.ok
    assert any("formatted strings" in v or "interpolated strings" in v for v in result.violations)


def test_postparse_metrics_do_not_recurse_even_on_an_invalid_parser_tree(monkeypatch):
    import ast
    node = ast.Name(id="x", ctx=ast.Load())
    for _ in range(900):
        node = ast.UnaryOp(op=ast.USub(), operand=node)
    monkeypatch.setattr(firewall.ast, "parse", lambda *a, **k: ast.Expression(body=node))
    result = firewall.check("x", ["x"])
    assert not result.ok
    assert result.depth == 21


@pytest.mark.parametrize("expression", [
    "cs_rank(1) + x", "ts_mean(1, 2) + x", "log1p(2) * x", "ts_std(x, 1)",
    "ts_mean(x, -1)", "ts_lag(x, 1+1)", "ts_lag(x, True)", "x + 1e999",
    "cs_rank + x", "ts_mean(x, 2, center=True)", "getattr(x, 'shape')",
    "x.__class__", "__import__('os')", "eval('x')", "x[0]",
])
def test_typed_operator_allowlist_rejects_invalid_expressions(expression):
    result = firewall.check(expression, ["x"])
    assert not result.ok, result.to_dict()


@pytest.mark.parametrize("name", ["cs_rank", "div", "features", "compute", "eval",
                                  "__x", "a__b", "a-b", "a.b", "1x", "class", "x\u212a"])
def test_feature_names_cannot_shadow_operators_or_generated_namespace(name):
    assert not firewall.check("x", ["x", name]).ok


def test_operator_implementation_change_invalidates_receipt(monkeypatch):
    before = operators.registry_sha256()
    monkeypatch.setitem(operators.REGISTRY, "cs_rank", operators.cs_demean)
    assert operators.registry_sha256() != before


def test_lowered_and_emitted_arithmetic_use_checked_division():
    index = pd.MultiIndex.from_product([pd.date_range("2024-01-01", periods=2), ["A", "B"]],
                                      names=["date", "asset"])
    features = {"x": pd.Series([2., 4., 6., 8.], index=index),
                "y": pd.Series([0., 2., np.inf, 4.], index=index)}
    expression = "-(x / y) + x * (2 + 3)"
    lowered = firewall.lower_expression(expression, list(features))
    assert "div(x, y)" in lowered and " / " not in lowered
    actual = eval(lowered, {"__builtins__": {}, **operators.REGISTRY}, features)
    expected = pd.Series([np.nan, 18., np.nan, 38.], index=index)
    pd.testing.assert_series_equal(actual, expected)
    namespace = {}
    exec(firewall.emit_code(expression, list(features)), namespace)
    pd.testing.assert_series_equal(namespace["compute"](features), expected)


def test_series_alignment_never_silently_joins_mismatched_rows():
    idx = pd.MultiIndex.from_product([[1, 2], ["A", "B"]], names=["date", "asset"])
    x = pd.Series([1., 2., 3., 4.], index=idx)
    with pytest.raises(operators.OperatorError, match="matching ordered keys"):
        operators.add(x, x.iloc[::-1])


def _hac_reference(values, lags):
    """Independent regression sandwich: missing slots are zero-response, zero-design rows.

    Fit y_t = observed_t * mu, without intercept. Missing rows have exactly zero residual
    and zero score. The OLS inverse cross-product is 1/n_observed, while cov_hac keeps the
    complete schedule for lag products. This is independent of the custom scalar loop.
    """
    values = np.asarray(values, dtype=float)
    observed = np.isfinite(values)
    model = OLS(np.where(observed, values, 0.), observed.astype(float)[:, None]).fit()
    return float(model.params[0]), float(np.sqrt(cov_hac(model, nlags=lags, use_correction=False)[0, 0]))


@pytest.mark.parametrize("values", [[1., np.nan, 2., 4., 3.],
                                   [1., 2., 4., 3., 8.],
                                   [np.nan, 3., 1., np.nan, 5., 7., np.nan],
                                   [-2., np.inf, 4., 3., 9.]])
@pytest.mark.parametrize("lags", [0, 1, 4, 8])
def test_calendar_hac_matches_independent_statsmodels_sandwich(values, lags):
    schedule = pd.bdate_range("2024-01-02", periods=len(values))
    x = pd.Series(values, index=schedule)
    expected_mean, expected_se = _hac_reference(values, lags)
    mean, se, t, n = C.newey_west(x, lags, scheduled_dates=schedule)
    assert mean == pytest.approx(expected_mean, rel=1e-13, abs=1e-14)
    assert se == pytest.approx(expected_se, rel=1e-13, abs=1e-14)
    assert t == pytest.approx(expected_mean / expected_se, rel=1e-13)
    assert n == int(np.isfinite(values).sum())


def test_missing_step_regression_has_hand_calculated_value():
    # mean=2.5, observed residuals [-1.5,-.5,1.5,.5]; only scheduled adjacent pairs
    # (-.5*1.5) + (1.5*.5) cancel. Meat=5; bread=1/4, SE=sqrt(5)/4.
    mean, se, t, n = C.newey_west(pd.Series([1., np.nan, 2., 4., 3.]), 1)
    assert (mean, n) == (2.5, 4)
    assert se == pytest.approx(np.sqrt(5.) / 4.)
    assert t == pytest.approx(10. / np.sqrt(5.))
    compressed = C.newey_west(pd.Series([1., 2., 4., 3.]), 1)[1]
    assert not np.isclose(se, compressed)


def test_sparse_observations_need_their_original_calendar():
    schedule = pd.bdate_range("2024-01-02", periods=5)
    sparse = pd.Series([1., 2., 4., 3.], index=schedule[[0, 2, 3, 4]])
    with pytest.raises(ValueError, match="scheduled_dates required"):
        C.newey_west(sparse, 1)
    assert C.newey_west(sparse, 1, scheduled_dates=schedule)[1] == pytest.approx(np.sqrt(5.) / 4.)


@pytest.mark.parametrize("schedule", [[0, 2, 1], [0, 1, 1], [0, 1, np.nan]])
def test_invalid_calendars_rejected(schedule):
    with pytest.raises(ValueError):
        C.newey_west(pd.Series([1., 2., 3.]), 1, scheduled_dates=pd.Index(schedule))


def test_estimates_outside_schedule_rejected():
    with pytest.raises(ValueError, match="outside the declared schedule"):
        C.newey_west(pd.Series([1., 2., 3.], index=[0, 1, 3]), 1,
                     scheduled_dates=pd.RangeIndex(3))


def _gate_context():
    dates = pd.bdate_range("2024-01-02", periods=5)
    assets = [f"A{i}" for i in range(8)]
    idx = pd.MultiIndex.from_product([dates, assets], names=["date", "asset"])
    s = np.array([1., 2., 3., 4., 5., 6., 7., 8.])
    z = np.array([3., 1., 4., 2., 8., 6., 7., 5.])
    returns = np.array([[8., 2., 7., 1., 6., 3., 5., 4.],
                        [np.nan] * 8,
                        [4., 3., 8., 1., 6., 2., 7., 5.],
                        [8., 3., 6., 2., 7., 4., 5., 1.],
                        [7., 1., 8., 2., 6., 5., 4., 3.]])
    scores = pd.Series(np.tile(s, 5), index=idx)
    control = pd.Series(np.tile(z, 5), index=idx)
    # Pure test thresholds: exercise estimators; they are not calibrated campaign policy.
    policy = {"minimum_assets_per_date": 5,
              "G4_rank_ic": {"min_dates": 3, "min_abs_mean_ic": 0., "min_abs_t_stat": 0.},
              "G7_partial_ic": {"control_k": 1, "min_dates": 3,
                                "min_abs_mean_partial_ic": 0., "min_abs_t_stat": 0.},
              "G8_fama_macbeth": {"min_dates": 3, "min_abs_t_stat": 0., "singular_design": "FAIL"},
              "G9_sign_alignment": {"hard_fail": True}}
    ctx = SimpleNamespace(scores=scores, returns=pd.Series(returns.ravel(), index=idx),
                          scheduled_dates=dates, horizon_days=2, policy=policy,
                          multiplicity_t_threshold=0., expected_sign=-1,
                          reference=Reference(z_asof={"z": control}, snapshot_id="numeric-fixture"),
                          artifacts={"novelty_similarities": {"z": {"similarity": .4}}})
    return ctx, s, z, returns


def _precision_partial_rank_correlation(s, r, z):
    # Independent identity: partial correlation is -P_xy/sqrt(P_xx P_yy), where P is
    # the inverse correlation matrix of the initially ranked variables. No residuals
    # and no production regression/correlation helper are used in this reference.
    ranked = np.column_stack([rankdata(s), rankdata(r), rankdata(z)])
    precision = np.linalg.inv(np.corrcoef(ranked.T))
    return -precision[0, 1] / np.sqrt(precision[0, 0] * precision[1, 1])


def test_g4_g7_g8_keep_grid_and_match_independent_numerical_references():
    ctx, scores, control, returns = _gate_context()
    results = [rank_ic.run(ctx), partial_ic.run(ctx), fama_macbeth.run(ctx)]
    expected_ic, expected_partial, expected_fm = [], [], []
    for r in returns:
        if not np.isfinite(r).all():
            expected_ic.append(np.nan)
            expected_partial.append(np.nan)
            expected_fm.append(np.nan)
            continue
        expected_ic.append(float(np.corrcoef(rankdata(scores), rankdata(r))[0, 1]))
        expected_partial.append(_precision_partial_rank_correlation(scores, r, control))
        # Closed-form normal equations, independent of implementation np.linalg.lstsq.
        design = np.column_stack([np.ones(len(scores)), scores, control])
        expected_fm.append(float(np.linalg.solve(design.T @ design, design.T @ r)[1]))
    for result, key, expected in zip(results, ["ic_series", "partial_ic_series", "fm_coef_series"],
                                     [expected_ic, expected_partial, expected_fm]):
        assert result.status == C.PASS, result.to_dict()
        series = ctx.artifacts[key]
        pd.testing.assert_index_equal(series.index, ctx.scheduled_dates)
        np.testing.assert_allclose(series.to_numpy(), expected, atol=1e-13)
        mean, se = _hac_reference(expected, 1)
        assert result.n_dates == 4
        assert result.estimate == pytest.approx(mean, abs=1e-13)
        assert result.uncertainty == pytest.approx(se, abs=1e-13)
        assert result.detail["missing_scheduled_dates"] == 1
        assert result.detail["hac_convention"] == C.HAC_CONVENTION


def test_g7_fully_spanned_score_never_manufactures_residual_sign():
    ctx, _, _, _ = _gate_context()
    ctx.scores = ctx.reference.reference_set()["z"].copy()
    result = partial_ic.run(ctx)
    assert result.status == C.INCONCLUSIVE
    assert ctx.artifacts["partial_ic_series"].isna().all()
    assert sign_alignment.run(ctx).status == C.FAIL


def test_g7_requires_the_complete_frozen_top_k():
    ctx, _, _, _ = _gate_context()
    ctx.policy["G7_partial_ic"]["control_k"] = 2
    assert partial_ic.run(ctx).status == C.INCONCLUSIVE
    assert fama_macbeth.run(ctx).status == C.INCONCLUSIVE


def test_g8_singular_date_hard_fails_and_cannot_be_softened():
    ctx, _, _, _ = _gate_context()
    partial_ic.run(ctx)
    first = ctx.scheduled_dates[0]
    ctx.scores.loc[first] = ctx.reference.reference_set()["z"].loc[first].to_numpy()
    assert fama_macbeth.run(ctx).status == C.FAIL
    ctx.policy["G8_fama_macbeth"]["singular_design"] = "SKIP"
    with pytest.raises(ValueError, match="must be FAIL"):
        fama_macbeth.run(ctx)


@pytest.mark.parametrize("estimates", list(product([-1., 0., 1.], repeat=3)) +
                         [(np.nan, 1., 1.), (1., np.inf, 1.), (1., 1., None)])
def test_g9_every_sign_conflict_or_undefined_value_hard_fails(estimates):
    ctx = SimpleNamespace(policy={"G9_sign_alignment": {"hard_fail": True}}, expected_sign=1,
                          artifacts=dict(zip(["mean_ic", "mean_partial_ic", "mean_fm_coef"], estimates)))
    expected = C.PASS if estimates == (1., 1., 1.) else C.FAIL
    assert sign_alignment.run(ctx).status == expected
