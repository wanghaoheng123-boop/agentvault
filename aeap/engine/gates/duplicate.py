"""G1 — Duplicate. Reject a repeated canonical expression hash, or an identical full
sorted (asset, date, value, missingness) output under the same snapshot.

Equal summary moments across DIFFERENT panels are not equality, so the comparison is on
the full aligned output, never on means and variances."""
from __future__ import annotations
import hashlib
import numpy as np
from ._common import Check, PASS, FAIL


def output_fingerprint(scores) -> str:
    s = scores.sort_index()
    parts = [f"{a}|{d}|{'' if not np.isfinite(v) else format(v, '.12g')}"
             for (d, a), v in s.items()]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def run(ctx) -> Check:
    expr_hash = ctx.expression_sha256
    fp = output_fingerprint(ctx.scores)
    for prior in ctx.reference.prior_candidates:
        if prior.get("expression_sha256") == expr_hash:
            return Check(FAIL, f"canonical expression identical to {prior.get('candidate_id')}",
                         detail={"match": "expression", "other": prior.get("candidate_id")})
        if prior.get("output_fingerprint") == fp:
            return Check(FAIL, f"output identical to {prior.get('candidate_id')} under this snapshot",
                         detail={"match": "output", "other": prior.get("candidate_id")})
    return Check(PASS, "no exact expression or output duplicate",
                 detail={"expression_sha256": expr_hash, "output_fingerprint": fp,
                         "compared_against": len(ctx.reference.prior_candidates)})
