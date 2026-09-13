"""Fingerprint decisions are journal-first and their fixtures cannot pass vacuously."""

from __future__ import annotations

import json

import pytest

from test_akspws import ak  # noqa: F401


def candidate(fid="FP-SECURE"):
    return {
        "schema_version": "1.0",
        "id": fid,
        "stage": "candidate",
        "rule": {"subject": "writes", "predicate": "writes require evidence",
                 "description": "Require evidence before writes"},
        "assertions": ["evidence"],
        "fixtures": [{"name": "description", "setup": {}, "expect": {"contains": "evidence"}}],
        "tags": ["security"],
    }


def write_candidate(av, doc, name="candidate.json"):
    path = av.ROOT / "MemoryBank" / "fingerprint" / "candidate" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_matches_predicate_is_never_a_vacuous_allow(ak):
    mod, _ = ak
    ok, detail = mod.fingerprint_eval._eval_expect(
        {"predicate": "something unrelated"}, {"action": "write"},
        {"matches_predicate": "allow"}, candidate())
    assert ok is False
    assert "not_an_executable" in detail


def test_refused_journal_causes_no_projection_side_effect(ak, av, monkeypatch):
    mod, Proxy = ak
    path = write_candidate(av, candidate())
    monkeypatch.setattr(mod.fingerprint_eval.commit_rebased, "commit_with_rebase",
                        lambda *a, **k: {"status": "error", "reason": "injected refusal"})
    result = mod.fingerprint_eval.promote_or_reject(Proxy, path)
    assert result["status"] == "error"
    assert path.exists()
    assert not list((path.parents[1] / "approved").glob("*.json"))
    assert not list((path.parents[1] / "proofs").glob("*.json"))


def test_evaluator_refuses_files_outside_candidate_staging(ak, av):
    mod, Proxy = ak
    outside = av.ROOT / "outside.json"
    outside.write_text(json.dumps(candidate()), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate/"):
        mod.fingerprint_eval.promote_or_reject(Proxy, outside)


def test_hydrate_reads_accepted_event_not_mutable_approved_file(ak, av):
    mod, Proxy = ak
    path = write_candidate(av, candidate())
    result = mod.fingerprint_eval.promote_or_reject(Proxy, path)
    assert result["status"] == "approved"
    approved = av.ROOT / "MemoryBank" / "fingerprint" / "approved" / "FP-SECURE.json"
    forged = json.loads(approved.read_text())
    forged["rule"]["description"] = "FORGED FILE CONTENT"
    approved.write_text(json.dumps(forged), encoding="utf-8")
    hydrated = mod.hydrate.hydrate(Proxy, tags=["security"])
    assert any("Require evidence before writes" in line for line in hydrated["rules"])
    assert all("FORGED" not in line for line in hydrated["rules"])


def test_reclaimed_lease_after_decision_blocks_fingerprint_materialization(
        ak, av, monkeypatch):
    mod, Proxy = ak
    path = write_candidate(av, candidate("FP-STALE-WRITER"), "stale.json")
    real_commit = mod.fingerprint_eval.commit_rebased.commit_with_rebase

    def commit_then_reclaim(*args, **kwargs):
        receipt = real_commit(*args, **kwargs)
        for lease_path in av.LEASES.glob("*.json"):
            lease = json.loads(lease_path.read_text())
            if lease.get("resources") == ["MemoryBank/**"]:
                lease["lease_token"] = "reclaimed-generation"
                av.atomic_write_json(lease_path, lease)
        return receipt

    monkeypatch.setattr(mod.fingerprint_eval.commit_rebased, "commit_with_rebase",
                        commit_then_reclaim)
    result = mod.fingerprint_eval.promote_or_reject(Proxy, path)
    assert result["status"] == "error" and "lease token/fence" in result["reason"]
    assert path.exists(), "a fenced-out worker must preserve the staged candidate"
    assert not (path.parents[1] / "approved" / "FP-STALE-WRITER.json").exists()
    assert not list((path.parents[1] / "proofs").glob("FP-STALE-WRITER-*.json"))
