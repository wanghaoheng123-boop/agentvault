"""Replay-only process contracts. Files are evidence, never production membership.

This ledger enforces sequencing and retention in an isolated fixture directory. It is not
an OS trust boundary: an ordinary process with the same filesystem permissions can read
fixtures. A final-label handle must remain in the evaluator process, not the generator.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

MODEL_VINTAGE = "MODEL_VINTAGE_CONTAMINATION_UNRESOLVED"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def date(value) -> str:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is not None:
        raise ValueError("decision dates must be finite timezone-naive calendar dates")
    if stamp != stamp.normalize():
        raise ValueError("decision dates must identify whole scheduled dates")
    return stamp.date().isoformat()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value):
        raise ValueError("invalid artifact or candidate identifier")
    return value


def series_payload(series: pd.Series) -> dict:
    if not isinstance(series.index, pd.MultiIndex) or series.index.names != ["date", "asset"]:
        raise ValueError("scores require a (date, asset) index")
    if series.index.has_duplicates or not series.index.is_monotonic_increasing:
        raise ValueError("scores require unique ordered keys")
    rows = []
    for (day, asset), val in series.items():
        if not pd.isna(val) and not math.isfinite(float(val)):
            raise ValueError("nonfinite score")
        rows.append([date(day), str(asset), None if pd.isna(val) else float(val)])
    return {"schema_version": "1.0", "rows": rows}


def series_from_payload(payload: dict) -> pd.Series:
    rows = payload["rows"]
    idx = pd.MultiIndex.from_tuples([(pd.Timestamp(d), a) for d, a, _ in rows],
                                  names=["date", "asset"])
    return pd.Series([float("nan") if v is None else v for _, _, v in rows], index=idx,
                     dtype="float64")


def immutable_json(path: Path, value: dict) -> str:
    data = canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"immutable artifact already exists with different bytes: {path}")
    return hashlib.sha256(data).hexdigest()


@dataclasses.dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    decision_dates: tuple[str, ...]
    final_start: str
    final_end: str
    trial_budget: int
    execution_budget: int
    baseline_execution_budget: int
    generator_budget: int
    feedback_budget: int
    policy_sha256: str
    generator_sha256: str
    corpus_sha256: str
    initial_library_sha256: str
    prompt_sha256: str
    code_sha256: str
    data_status: str = "SYNTHETIC_NOT_PIT"
    model_vintage_status: str = MODEL_VINTAGE
    schema_version: str = "1.0"

    def __post_init__(self):
        identifier(self.campaign_id)
        ds = tuple(date(d) for d in self.decision_dates)
        if not ds or tuple(sorted(set(ds))) != ds:
            raise ValueError("decision dates must be unique and strictly increasing")
        if date(self.final_start) <= ds[-1] or date(self.final_end) < date(self.final_start):
            raise ValueError("final holdout must follow every discovery decision")
        for name in ("trial_budget", "execution_budget", "baseline_execution_budget",
                     "generator_budget", "feedback_budget"):
            n = getattr(self, name)
            if type(n) is not int or n < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("policy_sha256", "generator_sha256", "corpus_sha256",
                     "initial_library_sha256", "prompt_sha256", "code_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)):
                raise ValueError(f"{name} requires a frozen SHA-256")
        object.__setattr__(self, "decision_dates", ds)
        object.__setattr__(self, "final_start", date(self.final_start))
        object.__setattr__(self, "final_end", date(self.final_end))

    def payload(self):
        result = dataclasses.asdict(self)
        result["decision_dates"] = list(result["decision_dates"])
        return result


class CampaignLedger:
    """Append-only, hash-linked replay evidence with budgets derived from events."""
    def __init__(self, root: Path, spec: CampaignSpec):
        root = Path(root).resolve()
        workspace = Path(__file__).resolve().parents[2].parent
        if root == workspace or workspace in root.parents or root in workspace.parents:
            raise ValueError("replay root must be isolated from the source workspace")
        self.root, self.spec = root, spec
        root.mkdir(parents=True, exist_ok=True)
        immutable_json(root / "campaign.json", spec.payload())
        (root / "events").mkdir(exist_ok=True)

    @contextlib.contextmanager
    def _lock(self):
        with (self.root / ".process.lock").open("a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
            fcntl.flock(f, fcntl.LOCK_UN)

    def events(self):
        if json.loads((self.root / "campaign.json").read_bytes()) != self.spec.payload():
            raise ValueError("campaign configuration changed after freeze")
        events, previous = [], "0" * 64
        for n, p in enumerate(sorted((self.root / "events").glob("*.json")), 1):
            obj = json.loads(p.read_bytes())
            stored = obj.pop("sha256")
            if obj["seq"] != n or obj["prev_sha256"] != previous or digest(obj) != stored:
                raise ValueError("invalid process journal prefix")
            if p.name != f"{n:08d}-{stored[:16]}.json":
                raise ValueError("process journal filename mismatch")
            obj["sha256"] = stored
            events.append(obj)
            previous = stored
        return events

    def _append(self, events, kind, body):
        obj = {"seq": len(events) + 1, "prev_sha256": events[-1]["sha256"] if events else "0" * 64,
               "kind": kind, "body": body, "campaign_sha256": digest(self.spec.payload())}
        obj["sha256"] = digest(obj)
        immutable_json(self.root / "events" / f"{obj['seq']:08d}-{obj['sha256'][:16]}.json", obj)
        return obj

    def record(self, kind: str, body: dict):
        with self._lock():
            ev = self.events()
            if any(e["kind"] == "final_opened" for e in ev):
                raise ValueError("final holdout already opened; campaign is permanently sealed")
            if kind != "final_opened" and any(e["kind"] == "closed" for e in ev):
                raise ValueError("campaign closed; no further tuning or feedback")
            if kind in {"generation", "trial", "feedback", "execution", "baseline_execution"}:
                limit = {"generation": self.spec.generator_budget, "trial": self.spec.trial_budget,
                         "feedback": self.spec.feedback_budget, "execution": self.spec.execution_budget,
                         "baseline_execution": self.spec.baseline_execution_budget}[kind]
                if sum(e["kind"] == kind for e in ev) >= limit:
                    raise ValueError(f"{kind} budget exhausted")
            if kind in {"generation", "trial"}:
                dd = date(body["decision_date"])
                if dd not in self.spec.decision_dates:
                    raise ValueError("decision date outside frozen campaign")
                previous = [e["body"]["decision_date"] for e in ev if e["kind"] in {"generation", "trial"}]
                if previous and dd < previous[-1]:
                    raise ValueError("decision dates moved backwards")
            if kind == "trial":
                parent = body.get("parent_trial")
                lineage = body.get("lineage", "candidate")
                if lineage not in {"candidate", "mutation", "repair", "policy_variant"}:
                    raise ValueError("unknown trial lineage")
                if lineage != "candidate" and not parent:
                    raise ValueError("mutations, repairs, and variants require parent trial")
                if parent and not any(e["kind"] == "trial" and e["sha256"] == parent for e in ev):
                    raise ValueError("trial parent is not a retained earlier trial")
            if kind == "final_opened" and not any(e["kind"] == "closed" for e in ev):
                raise ValueError("close campaign before opening final labels")
            if kind not in {"generation", "trial", "feedback", "execution", "baseline_execution",
                            "result", "lesson", "admission", "closed", "final_opened"}:
                raise ValueError("unsupported process event")
            return self._append(ev, kind, body)

    def scores(self, series: pd.Series) -> dict:
        if len(series) == 0 or not series.notna().any():
            raise ValueError("cannot retain/admit an empty or all-missing score artifact")
        payload = series_payload(series)
        sha = digest(payload)
        path = self.root / "scores" / f"{sha}.json"
        immutable_json(path, payload)
        return {"path": str(path.relative_to(self.root)), "sha256": sha, "rows": len(series)}

    def read_scores(self, ref: dict):
        path = (self.root / ref["path"]).resolve()
        if self.root not in path.parents or path.parent != self.root / "scores":
            raise ValueError("score artifact escapes evidence store")
        payload = json.loads(path.read_bytes())
        if digest(payload) != ref["sha256"] or len(payload["rows"]) != ref["rows"]:
            raise ValueError("score artifact content changed")
        return series_from_payload(payload)

    def close(self, frozen_factor_hashes: list[str]):
        return self.record("closed", {"frozen_factor_hashes": sorted(frozen_factor_hashes)})

    def open_final(self, evidence: dict):
        """Evaluator supplies aggregate evidence; final labels are never logged as feedback."""
        required = {"window", "uncertainty", "attempt_count", "process", "baseline", "label_sha256"}
        if not required <= evidence.keys():
            raise ValueError("incomplete released final-process evidence")
        if evidence["window"] != {"start": self.spec.final_start, "end": self.spec.final_end}:
            raise ValueError("final evidence window differs from preregistration")
        trials = sum(e["kind"] == "trial" for e in self.events())
        if evidence["attempt_count"] != trials:
            raise ValueError("report omitted campaign attempts")
        event = self.record("final_opened", evidence)
        report = {"schema_version": "1.0", "campaign_id": self.spec.campaign_id,
                  "campaign_sha256": digest(self.spec.payload()), "release_sha256": event["sha256"],
                  "headline_evidence": "released_final_process_test", "final_evidence": evidence,
                  "production_admissions": 0, "data_status": self.spec.data_status,
                  "model_vintage_status": self.spec.model_vintage_status,
                  "historical_implementability": False, "net_implementability": False,
                  "combined_portfolio_evaluated": False, "null_results_valid": True,
                  "compute_and_search_budget": self.spec.payload(),
                  "readiness": "SYNTHETIC_MECHANICS_ONLY"}
        immutable_json(self.root / "released-final-report.json", report)
        return report
