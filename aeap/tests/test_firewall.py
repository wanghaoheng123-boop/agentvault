"""RFC T12 — proposal/firewall. Everything here must be rejected BEFORE any data handle
exists, which is why these tests never construct a panel."""
from __future__ import annotations
import pytest
from aeap.engine import firewall

FEATS = ["x", "momentum", "size"]


def ok(expr):
    return firewall.check(expr, FEATS, candidate_id="T")


@pytest.mark.parametrize("expr", [
    "cs_rank(x)",
    "cs_rank(ts_mean(x, 5))",
    "add(cs_rank(x), cs_zscore(momentum))",
    "x - momentum",
    "div(x, size)",
    "cs_rank(ts_delta(x, 21)) * 2",
    "log1p(cs_zscore(size))",
])
def test_valid_expressions_pass(expr):
    r = ok(expr)
    assert r.ok, r.violations


@pytest.mark.parametrize("expr,needle", [
    ("__import__('os').system('ls')", "only direct calls"),
    ("import os", "syntax error"),
    ("eval('1+1')", "not in policy allowlist"),
    ("getattr(x, 'values')", "not in policy allowlist"),
    ("x.__class__", "disallowed syntax"),
    ("x.rolling(5).mean()", "only direct calls"),
    ("x[0]", "disallowed syntax"),
    ("[i for i in x]", "disallowed syntax"),
    ("lambda a: a", "disallowed syntax"),
    ("ts_lag(x, -1)", "negative window"),
    ("ts_lag(x, 0 - 1)", "constant int literal"),
    ("ts_mean(x, n)", "constant int literal"),
    ("ts_mean(x, 5000)", "above window_max"),
    ("unknown_feature + x", "undeclared feature"),
    ("open('/etc/passwd')", "not in policy allowlist"),
    ("cs_rank('close')", "only numeric constants"),
    ("ts_mean(x, 5, 6)", "expected 2 args"),
    ("cs_rank(x=1)", "keyword arguments"),
    ("42", "references no declared feature"),
    ("", "empty expression"),
])
def test_hostile_and_malformed_are_rejected(expr, needle):
    r = ok(expr)
    assert not r.ok, f"{expr!r} should have been rejected"
    assert any(needle in v for v in r.violations), (expr, r.violations)


def test_oversized_expression_rejected_before_parsing():
    r = ok("x + " * 5000 + "x")
    assert not r.ok
    assert any("max_source_bytes" in v for v in r.violations)


def test_deeply_nested_expression_rejected():
    r = ok("cs_rank(" * 40 + "x" + ")" * 40)
    assert not r.ok
    assert any("max_nodes" in v or "depth" in v for v in r.violations)


def test_label_column_is_not_reachable():
    """Returns are never a declared feature, so naming one is an undeclared-name error."""
    r = ok("cs_rank(fwd_excess)")
    assert not r.ok
    assert any("undeclared feature" in v for v in r.violations)


def test_receipt_binds_expression_and_operator_version():
    a = ok("cs_rank(x)")
    b = ok("cs_rank(momentum)")
    assert firewall.receipt_sha256(a) != firewall.receipt_sha256(b)
    assert a.operator_registry_sha256 == b.operator_registry_sha256


def test_emitted_code_is_generated_not_authored():
    code = firewall.emit_code("cs_rank(x)", FEATS)
    assert "GENERATED" in code and "return cs_rank(x)" in code
