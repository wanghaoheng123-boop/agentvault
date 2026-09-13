"""G9 — Sign alignment. HARD FAIL.

Require sign(mean IC) == sign(mean partial-IC) == sign(mean FM coefficient) ==
preregistered expected_sign. Zero and undefined are NOT agreement.

This cannot be downgraded by narrative, and it is the check that catches a candidate whose
magnitudes look fine while its conditional and unconditional relationships point in
opposite directions. Changing the orientation after seeing results creates a NEW candidate
and debits the campaign budget; it never rescues this one."""
from __future__ import annotations
from ._common import Check, PASS, FAIL, need, sign_of


def run(ctx) -> Check:
    if not bool(need(ctx.policy, "G9_sign_alignment", "hard_fail")):
        raise ValueError("G9 hard_fail cannot be disabled")
    want = ctx.expected_sign
    got = {
        "mean_ic": sign_of(ctx.artifacts.get("mean_ic")),
        "mean_partial_ic": sign_of(ctx.artifacts.get("mean_partial_ic")),
        "mean_fm_coef": sign_of(ctx.artifacts.get("mean_fm_coef")),
    }
    detail = {"expected_sign": want, "observed_signs": got,
              "estimates": {k: ctx.artifacts.get(k)
                            for k in ("mean_ic", "mean_partial_ic", "mean_fm_coef")}}
    missing = [k for k, v in got.items() if v == 0]
    if missing:
        return Check(FAIL, f"{missing} have zero or undefined sign; that is not agreement",
                     detail=detail)
    disagree = [k for k, v in got.items() if v != want]
    if disagree:
        return Check(FAIL, f"{disagree} disagree with preregistered sign {want}", detail=detail)
    return Check(PASS, f"IC, partial-IC and FM coefficient all agree with sign {want}", detail=detail)
