"""RFC T13 (sandbox containment) and T17 (point-in-time availability)."""
from __future__ import annotations
import hashlib
import numpy as np
import pandas as pd
import pytest
from aeap.engine import panels, sandbox

FEATS = ["x", "momentum", "size"]


def feats(**kw):
    f, r, g, e, s = panels.make_panel(n_dates=60, n_assets=20, seed=3, signal=0.3, **kw)
    return {k: f[k] for k in FEATS}, r, s


# ------------------------------------------------------------------ T13 containment

def test_execution_is_refused_without_a_firewall_pass():
    fx, _, _ = feats()
    scores, m = sandbox.execute("cs_rank(x)", fx, candidate_id="C",
                                executor_run_id="re", firewall_passed=False)
    assert scores is None and m["status"] == "refused"
    assert "firewall" in m["refusal_reason"]


def test_labels_are_not_in_the_execution_namespace():
    """The judge is the first component that sees returns; the executor never does."""
    fx, _, _ = feats()
    scores, m = sandbox.execute("cs_rank(fwd_excess)", fx, candidate_id="C",
                                executor_run_id="re", firewall_passed=True)
    assert scores is None and m["status"] == "refused"


def test_builtins_are_absent_from_the_namespace():
    fx, _, _ = feats()
    for hostile in ("__import__('os').getcwd()", "open('/etc/passwd').read()"):
        scores, m = sandbox.execute(hostile, fx, candidate_id="C",
                                    executor_run_id="re", firewall_passed=True)
        assert scores is None, hostile
        assert m["status"] == "refused"


def test_forged_firewall_flag_cannot_invoke_series_methods(tmp_path, monkeypatch):
    fx, _, _ = feats()
    escaped = tmp_path / "executor-must-not-create-this.pkl"
    called = {"child": False}

    def child_must_not_start(*args, **kwargs):
        called["child"] = True
        raise AssertionError("rejected AST reached the child")

    monkeypatch.setattr(sandbox.subprocess, "run", child_must_not_start)
    expression = f"x.to_pickle({str(escaped)!r})"
    scores, manifest = sandbox.execute(
        expression, fx, candidate_id="FORGED", executor_run_id="re",
        firewall_passed=True,
    )
    assert scores is None and manifest["status"] == "refused"
    assert "executor firewall rejected" in manifest["refusal_reason"]
    assert not called["child"] and not escaped.exists()


def test_executor_does_not_write_bytecode_into_code_tree():
    fx, _, _ = feats()

    def snapshot():
        return {
            str(path.relative_to(sandbox.ENGINE_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sandbox.ENGINE_ROOT.rglob("*.pyc")
        }

    before = snapshot()
    scores, manifest = sandbox.execute(
        "cs_rank(x)", fx, candidate_id="READONLY-CODE", executor_run_id="re",
        firewall_passed=True,
    )
    assert scores is not None and manifest["status"] == "ok"
    assert snapshot() == before


def test_manifest_never_claims_to_be_a_security_boundary():
    fx, _, _ = feats()
    scores, m = sandbox.execute("cs_rank(x)", fx, candidate_id="C",
                                executor_run_id="re", firewall_passed=True)
    assert m["status"] == "ok"
    assert m["isolation"]["is_security_boundary"] is False
    assert m["isolation"]["network"] is False
    assert m["isolation"]["labels_visible"] is False


def test_unenforced_limits_are_reported_not_hidden():
    """A limit the platform refused must appear in limits_skipped, never be implied applied."""
    fx, _, _ = feats()
    _, m = sandbox.execute("cs_rank(x)", fx, candidate_id="C",
                           executor_run_id="re", firewall_passed=True)
    iso = m["isolation"]
    assert "limits_applied" in iso and "limits_skipped" in iso
    assert set(iso["limits_applied"]) & {"RLIMIT_CPU", "RLIMIT_FSIZE"}


def test_output_key_uniqueness_is_validated():
    fx, _, _ = feats()
    scores, m = sandbox.execute("cs_rank(x)", fx, candidate_id="C",
                                executor_run_id="re", firewall_passed=True)
    assert m["status"] == "ok"
    assert not scores.index.duplicated().any()
    assert m["row_count"] == len(scores)


def test_output_hash_is_recorded_and_deterministic():
    fx, _, _ = feats()
    _, m1 = sandbox.execute("cs_rank(x)", fx, candidate_id="C", executor_run_id="re",
                            firewall_passed=True)
    _, m2 = sandbox.execute("cs_rank(x)", fx, candidate_id="C", executor_run_id="re",
                            firewall_passed=True)
    assert m1["output_sha256"] == m2["output_sha256"]


# ------------------------------------------------------------------------- T17 PIT

def test_adding_future_rows_does_not_change_historical_scores():
    """The empirical look-ahead test. If a past score moves when future data arrives, the
    candidate reads the future regardless of what its AST looked like."""
    short, _, sched_short = feats()
    f_long, _, _, _, _ = panels.make_panel(n_dates=120, n_assets=20, seed=3, signal=0.3, horizon=5)
    long = {k: f_long[k] for k in FEATS}
    hist = short["x"].index
    res = sandbox.invariance_check("cs_rank(ts_mean(x, 5))", short, long, hist)
    assert res["status"] == "PASS", res


def test_trailing_operators_never_read_forward():
    from aeap.engine import operators as ops
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-01", periods=6, freq="B"), ["A"]], names=["date", "asset"])
    s = pd.Series([1.0, 2, 3, 4, 5, 6], index=idx)
    lagged = ops.ts_lag(s, 1)
    assert np.isnan(lagged.iloc[0])
    assert list(lagged.dropna().to_numpy()) == [1, 2, 3, 4, 5]
    m = ops.ts_mean(s, 3)
    assert np.isnan(m.iloc[0]) and np.isnan(m.iloc[1])
    assert m.iloc[2] == pytest.approx(2.0)


def test_negative_window_is_refused_at_runtime_too():
    from aeap.engine import operators as ops
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-01", periods=3, freq="B"), ["A"]], names=["date", "asset"])
    s = pd.Series([1.0, 2, 3], index=idx)
    with pytest.raises(ops.OperatorError, match="negative window"):
        ops.ts_lag(s, -1)


def test_division_by_zero_is_nan_not_inf():
    from aeap.engine import operators as ops
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2020-01-01", periods=2, freq="B"), ["A"]], names=["date", "asset"])
    a = pd.Series([1.0, 1.0], index=idx)
    b = pd.Series([0.0, 2.0], index=idx)
    out = ops.div(a, b)
    assert np.isnan(out.iloc[0]) and out.iloc[1] == 0.5
    assert not np.isinf(out).any()


def test_cs_rank_is_scoped_per_date():
    from aeap.engine import operators as ops
    dates = pd.date_range("2020-01-01", periods=2, freq="B")
    idx = pd.MultiIndex.from_product([dates, ["A", "B"]], names=["date", "asset"])
    s = pd.Series([1.0, 2.0, 100.0, 200.0], index=idx)
    r = ops.cs_rank(s)
    # Ranks reset each date: the huge second-day values do not dominate the first day.
    assert r.loc[(dates[0], "A")] == r.loc[(dates[1], "A")]


# ------------------------------------------------- dev adapter is barred from admission

def test_yfinance_adapter_declares_itself_non_pit():
    from aeap.engine.adapters import yfinance_dev as yf
    assert "survivorship" in yf.BIASES and "adjusted_prices" in yf.BIASES
    assert yf.data_status({"pit_compliant": False}) == "DEV_ONLY_NOT_PIT"
    assert yf.data_status({"pit_compliant": True}) == "PIT"
