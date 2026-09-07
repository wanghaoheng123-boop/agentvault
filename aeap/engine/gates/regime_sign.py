"""G5 — Regime sign. Regimes and their availability are declared before testing.

Every required regime with enough support must have a nonzero mean IC in the preregistered
direction. A required regime WITHOUT enough support is INCONCLUSIVE, not a pass: absence of
evidence in a regime we committed to testing is not evidence of consistency."""
from __future__ import annotations
import numpy as np
from ._common import Check, PASS, FAIL, INCONCLUSIVE, need, sign_of


def run(ctx) -> Check:
    required = list(need(ctx.policy, "G5_regime_sign", "required_regimes"))
    min_dates = int(need(ctx.policy, "G5_regime_sign", "min_dates_per_regime"))
    ic = ctx.artifacts.get("ic_series")
    if ic is None or ic.dropna().empty:
        return Check(INCONCLUSIVE, "no IC series available (G4 did not produce one)")
    if ctx.regimes is None:
        return Check(INCONCLUSIVE, "no regime labels supplied by the reference snapshot")

    want = ctx.expected_sign
    per, offending, unsupported = {}, [], []
    for name in required:
        dates = ctx.regimes.index[ctx.regimes == name]
        sub = ic.reindex(dates).dropna()
        if len(sub) < min_dates:
            unsupported.append(name)
            per[name] = {"n_dates": int(len(sub)), "mean_ic": None, "status": "unsupported"}
            continue
        m = float(sub.mean())
        per[name] = {"n_dates": int(len(sub)), "mean_ic": m, "status": "ok"}
        if sign_of(m) != want:
            offending.append(name)

    detail = {"per_regime": per, "expected_sign": want, "min_dates_per_regime": min_dates}
    if offending:
        return Check(FAIL, f"regime(s) {offending} have mean IC not in the preregistered direction",
                     detail=detail)
    if unsupported:
        return Check(INCONCLUSIVE, f"required regime(s) {unsupported} lack {min_dates} supported dates",
                     detail=detail)
    return Check(PASS, f"all required regimes agree with preregistered sign {want}", detail=detail)
