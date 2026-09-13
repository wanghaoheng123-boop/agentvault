#!/usr/bin/env python3
"""Deterministic fingerprint promotion: Candidate → Approved | Rejected.

Gates:
  1. Schema / required fields
  2. Conflict detector vs existing Approved rules
  3. Empirical fixtures — 100% pass required
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import commit_rebased, store

REQUIRED_CANDIDATE = (
    "schema_version",
    "id",
    "stage",
    "rule",
    "assertions",
    "fixtures",
)


def _fp_root(av: Any) -> Path:
    return store.paths_from_av(av)["fingerprint"]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema(doc: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    for k in REQUIRED_CANDIDATE:
        if k not in doc:
            errs.append(f"missing_field:{k}")
    if doc.get("stage") not in ("candidate", "Candidate"):
        errs.append(f"stage_must_be_candidate:{doc.get('stage')!r}")
    rule = doc.get("rule")
    if not isinstance(rule, dict):
        errs.append("rule_must_be_object")
    else:
        if not rule.get("predicate") and not rule.get("description"):
            errs.append("rule_needs_predicate_or_description")
    if not isinstance(doc.get("assertions"), list):
        errs.append("assertions_must_be_list")
    if not isinstance(doc.get("fixtures"), list) or len(doc.get("fixtures") or []) < 1:
        errs.append("fixtures_must_be_nonempty_list")
    else:
        for i, fix in enumerate(doc["fixtures"]):
            if not isinstance(fix, dict) or "name" not in fix or "expect" not in fix:
                errs.append(f"fixture[{i}]_needs_name_and_expect")
    fid = doc.get("id") or ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", str(fid)):
        errs.append(f"bad_id:{fid!r}")
    return errs


def _normalize_predicate(pred: str) -> str:
    return re.sub(r"\s+", " ", (pred or "").strip().lower())


def _accepted_rules(av: Any) -> list[dict[str, Any]]:
    """Rules come from accepted journal events; files under approved/ are projections."""
    cw = commit_rebased._load_commit_worker(av)
    rules: dict[str, dict[str, Any]] = {}
    for event in commit_rebased.journal_events(cw):
        if event["type"] != "fingerprint.promoted":
            continue
        body = event.get("body") or {}
        doc = body.get("rule_document")
        if isinstance(doc, dict) and isinstance(body.get("fingerprint_id"), str):
            rules[body["fingerprint_id"]] = doc
    return [rules[key] for key in sorted(rules)]


def detect_conflicts(av: Any, candidate: dict[str, Any]) -> list[str]:
    """Semantic conflicts: same id, or contradictory predicates on same subject."""
    conflicts: list[str] = []
    pred = _normalize_predicate((candidate.get("rule") or {}).get("predicate") or "")
    subject = (candidate.get("rule") or {}).get("subject") or candidate.get("id")
    neg = pred.startswith("not ") or " must not " in pred or " never " in pred

    for other in _accepted_rules(av):
        if other.get("id") == candidate.get("id"):
            conflicts.append(f"duplicate_id:{candidate.get('id')}")
            continue
        opred = _normalize_predicate((other.get("rule") or {}).get("predicate") or "")
        osubj = (other.get("rule") or {}).get("subject") or other.get("id")
        if subject and osubj and subject == osubj and pred and opred:
            o_neg = opred.startswith("not ") or " must not " in opred or " never " in opred
            # Strip leading 'not ' for core comparison.
            core = pred[4:] if pred.startswith("not ") else pred
            ocore = opred[4:] if opred.startswith("not ") else opred
            if core == ocore and neg != o_neg:
                conflicts.append(f"contradiction:{other.get('id')}:{opred!r}_vs_{pred!r}")
            # Explicit contradicts list on candidate.
        for c in candidate.get("contradicts") or []:
            if c == other.get("id"):
                conflicts.append(f"explicit_contradicts:{c}")
    return conflicts


def run_fixtures(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Run deterministic fixtures. Each fixture.expect is matched against evaluate().

    Fixture shapes:
      {"name", "setup": {...}, "expect": {...}}
      expect may include: {"ok": true}, {"equals": {"path": "a.b", "value": ...}},
      {"contains": "substr"} against rule.description, {"assert_true": "setup.key"}
    """
    results: list[dict[str, Any]] = []
    rule = candidate.get("rule") or {}
    for fix in candidate.get("fixtures") or []:
        name = fix.get("name", "?")
        setup = fix.get("setup") or {}
        expect = fix.get("expect") or {}
        try:
            ok, detail = _eval_expect(rule, setup, expect, candidate)
            results.append({"name": name, "pass": ok, "detail": detail})
        except Exception as e:
            results.append({"name": name, "pass": False, "detail": f"exception:{e}"})
    return results


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _eval_expect(
    rule: dict[str, Any],
    setup: dict[str, Any],
    expect: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, str]:
    if "ok" in expect:
        # Synthetic: rule.predicate must be non-empty when ok=true.
        want = bool(expect["ok"])
        got = bool(rule.get("predicate") or rule.get("description"))
        return (got == want), f"ok:{got}"
    if "equals" in expect:
        spec = expect["equals"]
        path = spec["path"]
        want = spec["value"]
        # Resolve from setup first, then rule, then candidate.
        got = _dig(setup, path)
        if got is None:
            got = _dig(rule, path)
        if got is None:
            got = _dig(candidate, path)
        return (got == want), f"equals:{path}:{got!r}=={want!r}"
    if "contains" in expect:
        substr = str(expect["contains"])
        hay = str(rule.get("description") or rule.get("predicate") or "")
        return (substr in hay), f"contains:{substr!r}"
    if "assert_true" in expect:
        key = expect["assert_true"]
        val = _dig(setup, key) if "." in key else setup.get(key)
        return (bool(val), f"assert_true:{key}={val!r}")
    if "assert_false" in expect:
        key = expect["assert_false"]
        val = _dig(setup, key) if "." in key else setup.get(key)
        return (not bool(val), f"assert_false:{key}={val!r}")
    if "matches_predicate" in expect:
        # Natural-language predicate execution is not a deterministic evaluator. Refuse the
        # shape rather than handing every `allow` fixture a vacuous success.
        return False, "matches_predicate_is_not_an_executable_fixture"
    return False, "unknown_expect_shape"


def evaluate_candidate(av: Any, path: Path) -> dict[str, Any]:
    doc = load_json(path)
    schema_errs = validate_schema(doc)
    conflicts = [] if schema_errs else detect_conflicts(av, doc)
    fixtures = [] if schema_errs or conflicts else run_fixtures(doc)
    all_pass = bool(fixtures) and all(f["pass"] for f in fixtures)
    passed = not schema_errs and not conflicts and all_pass
    return {
        "id": doc.get("id"),
        "path": str(path),
        "passed": passed,
        "schema_errors": schema_errs,
        "conflicts": conflicts,
        "fixtures": fixtures,
        "doc": doc,
    }


def promote_or_reject(
    av: Any,
    path: Path,
    *,
    agent_id: str = "orchestrator",
    run_id: str = "",
    run_token: str = "",
    lease_proofs: Any = None,
) -> dict[str, Any]:
    """Commit the decision first; materialized files and SQLite remain projections."""
    root = _fp_root(av)
    candidate_dir = (root / "candidate").resolve()
    resolved = path.resolve()
    if path.is_symlink() or not path.is_file() or resolved.parent != candidate_dir:
        raise ValueError("fingerprint evaluation accepts one regular file directly under candidate/")
    report = evaluate_candidate(av, path)
    source_bytes = path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    raw_id = report.get("id")
    fid = raw_id if isinstance(raw_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,63}", raw_id) else f"INVALID-{source_hash[:12]}"
    decision = "approved" if report["passed"] else "rejected"
    doc = dict(report["doc"])
    doc["stage"] = decision
    proof = {
        "schema_version": "2.0",
        "id": fid,
        "candidate_sha256": source_hash,
        "passed": report["passed"],
        "schema_errors": report["schema_errors"],
        "conflicts": report["conflicts"],
        "fixtures": report["fixtures"],
        "source": str(path.relative_to(av.ROOT)),
    }
    proof_hash = hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    proof_path = root / "proofs" / f"{fid}-{proof_hash[:16]}.json"
    dest = root / decision / f"{fid}.json"
    body = {"fingerprint_id": fid, "decision": decision, "candidate_sha256": source_hash,
            "rule_document": doc, "proof_document": proof,
            "proof": str(proof_path.relative_to(av.ROOT)), "dest": str(dest.relative_to(av.ROOT))}
    journal = commit_rebased.commit_with_rebase(
        av, "fingerprint.promoted" if report["passed"] else "fingerprint.rejected", body,
        agent_id=agent_id,
        resources=[str(path.relative_to(av.ROOT)), str(dest.relative_to(av.ROOT)),
                   str(proof_path.relative_to(av.ROOT)), "MemoryBank/coord/kernel/**"],
        payloads=[doc, proof], expected={},
        idempotency_key=f"fp-{decision}-{fid}-{source_hash}",
        run_id=run_id, run_token=run_token, lease_proofs=lease_proofs,
    )
    if journal.get("status") not in {"committed", "duplicate"}:
        return {"status": "error", "id": fid, "reason": journal.get("reason", "journal refused decision"),
                "report": {k: v for k, v in report.items() if k != "doc"}, "journal": journal}

    try:
        cw = commit_rebased._load_commit_worker(av)
        materialization_resources = [
            str(path.relative_to(av.ROOT)), str(dest.relative_to(av.ROOT)),
            str(proof_path.relative_to(av.ROOT)), "MemoryBank/coord/kernel/**",
        ]
        # The journal commit point and these caches are separate by design. Revalidate
        # the exact lease generations under the global writer lock before every
        # post-commit filesystem/database write so a reclaimed worker cannot publish.
        with cw.av.coord_lock():
            cw.authorize(materialization_resources, agent_id, run_id, run_token, lease_proofs)
            accepted = commit_rebased.journal_events(cw)
            match = next((event for event in accepted if event["seq"] == journal["seq"]), None)
            if (match is None or match.get("hash") != journal.get("hash")
                    or match.get("body") != body):
                raise RuntimeError("accepted fingerprint receipt no longer matches journal")
            if (path.is_symlink() or not path.is_file()
                    or path.resolve().parent != candidate_dir
                    or hashlib.sha256(path.read_bytes()).hexdigest() != source_hash):
                raise RuntimeError("candidate changed after journal decision; refusing materialization")

            materialized_doc = dict(doc)
            stamp = match["ts"]
            materialized_doc[f"{decision}_at"] = stamp
            materialized_doc["proof"] = str(proof_path.relative_to(av.ROOT))
            for target, expected_doc in ((proof_path, proof), (dest, materialized_doc)):
                if target.exists():
                    if target.is_symlink() or not target.is_file() or load_json(target) != expected_doc:
                        raise RuntimeError(f"fingerprint projection collision: {target}")
                else:
                    av.atomic_write_json(target, expected_doc)
            if path.resolve() != dest.resolve():
                path.unlink()
                av.fsync_dir(path.parent)
            store.ensure_kernel_dirs(av)
            conn = store.connect(av)
            try:
                store.index_fingerprint(
                    conn, fid=fid, stage=decision, path=str(dest.relative_to(av.ROOT)),
                    version=1, ts=stamp,
                )
            finally:
                conn.close()
    except (ValueError, OSError, RuntimeError, PermissionError) as error:
        return {"status": "error", "id": fid, "reason": str(error),
                "report": {k: v for k, v in report.items() if k != "doc"}, "journal": journal}
    return {"status": decision, "id": fid, "dest": str(dest), "proof": str(proof_path),
            "report": {k: v for k, v in report.items() if k != "doc"}, "journal": journal}


def status(av: Any) -> dict[str, Any]:
    root = _fp_root(av)
    events = commit_rebased.journal_events(commit_rebased._load_commit_worker(av))
    promoted = {e["body"].get("fingerprint_id") for e in events if e["type"] == "fingerprint.promoted"}
    rejected = {e["body"].get("fingerprint_id") for e in events if e["type"] == "fingerprint.rejected"}
    def count(sub: str) -> int:
        d = root / sub
        return len(list(d.glob("*.json"))) if d.exists() else 0
    return {
        "hypothesis": count("hypothesis"),
        "candidate": count("candidate"),
        "approved": len(promoted - {None}),
        "rejected": len(rejected - {None}),
        "proofs": count("proofs"),
    }
