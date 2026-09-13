"""Reference set for G1/G6/G7 — the novelty librarian's frozen view.

`Z_asof` is whatever the reference snapshot said was known AT the decision date. Future zoo
entries are simply absent from the object, so a later discovery cannot leak into an earlier
evaluation by construction rather than by discipline."""
from __future__ import annotations
import hashlib
import json
from .process_contract import series_payload
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class Reference:
    z_asof: dict[str, pd.Series] = field(default_factory=dict)
    prior_admitted: dict[str, pd.Series] = field(default_factory=dict)
    prior_candidates: list[dict] = field(default_factory=list)
    exclude: str | None = None
    snapshot_id: str = "ref-empty"

    def reference_set(self) -> dict[str, pd.Series]:
        """U = Z_asof UNION prior_admitted, excluding this candidate."""
        u = dict(self.z_asof)
        u.update(self.prior_admitted)
        if self.exclude:
            u.pop(self.exclude, None)
        return u

    def sha256(self) -> str:
        payload = {
            "snapshot_id": self.snapshot_id,
            "scores": {name: series_payload(scores) for name, scores in sorted(self.reference_set().items())},
            "priors": sorted(self.prior_candidates, key=lambda p: p.get("candidate_id", "")),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
