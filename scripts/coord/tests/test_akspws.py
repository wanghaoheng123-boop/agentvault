"""AK-SPWS — rebase CAS, compaction integrity, fingerprint promotion."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import SRC, expected_head


@pytest.fixture
def cw(av, writer_credentials):
    spec = importlib.util.spec_from_file_location("cw_ak", SRC / "commit_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cw_ak"] = mod
    spec.loader.exec_module(mod)
    mod.av.configure_paths(av.ROOT)
    # Appends are authenticated now: pin the head the caller actually observed, unless the
    # test is deliberately asserting on a stale expectation.
    original = mod.commit

    def authenticated_commit(event_type, body, **kw):
        kw.setdefault("expected", expected_head(mod, event_type, body, kw.get("task_id", "")))
        return original(event_type, body, **kw)

    mod.commit = authenticated_commit
    return mod


@pytest.fixture
def ak(av, monkeypatch, writer_credentials):
    """Load avkernel package with avcoord ROOT bound and live writer credentials.

    commit_rebased reads AVCOORD_RUN_ID / AVCOORD_RUN_TOKEN / AVCOORD_LEASE_PROOFS_JSON from
    the environment, so kernel entry points need the same registered run as a direct append.
    """
    # Load package via path without a top-level `import avkernel` (import_resolve gate).
    pkg_init = SRC / "avkernel" / "__init__.py"
    for name in list(sys.modules):
        if name == "avkernel" or name.startswith("avkernel."):
            del sys.modules[name]
    # Register package root so relative imports inside avkernel work.
    pkg_spec = importlib.util.spec_from_file_location(
        "avkernel",
        pkg_init,
        submodule_search_locations=[str(SRC / "avkernel")],
    )
    avkernel = importlib.util.module_from_spec(pkg_spec)
    sys.modules["avkernel"] = avkernel
    assert pkg_spec.loader is not None
    pkg_spec.loader.exec_module(avkernel)

    class Proxy:
        ROOT = av.ROOT
        COORD = av.COORD
        iso = staticmethod(av.iso)
        atomic_write_json = staticmethod(av.atomic_write_json)
        atomic_write_text = staticmethod(av.atomic_write_text)
        fsync_dir = staticmethod(av.fsync_dir)
        FileLock = av.FileLock
        audit = staticmethod(av.audit)
        load_json = staticmethod(av.load_json)
        canon_resource = staticmethod(av.canon_resource)
        CanonError = av.CanonError
        __file__ = str(SRC / "avcoord.py")

    paths = avkernel.store.ensure_kernel_dirs(Proxy)
    avkernel.store.write_config(paths["config"], {
        **avkernel.store.DEFAULT_CONFIG,
        "compact_every_n": 3,
        "compact_max_bytes": 50_000_000,
        "rebase_max_retries": 5,
    })
    proto = av.COORD / "protocol.json"
    if not proto.exists():
        proto.write_text(json.dumps({"schema_version": "1.0", "epoch": 1, "authority": "legacy"}))
    return avkernel, Proxy


def test_expected_cas_rejects_stale_journal_seq(cw, av):
    cw.av.configure_paths(av.ROOT)
    (av.COORD / "protocol.json").write_text(
        json.dumps({"schema_version": "1.0", "epoch": 1, "authority": "legacy"})
    )
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    with pytest.raises(RuntimeError, match="expected CAS failed"):
        cw.commit(
            "task.transition",
            {"task_id": "T", "status": "done"},
            expected={"journal_seq": 0},
        )


def test_commit_with_rebase_pins_and_succeeds(ak, cw, av):
    ak_mod, Proxy = ak
    cw.av.configure_paths(av.ROOT)
    r = ak_mod.commit_rebased.commit_with_rebase(
        Proxy,
        "task.transition",
        {"task_id": "T1", "status": "open"},
        expected={},
        agent_id="orchestrator",
    )
    assert r["status"] == "committed"
    assert r["seq"] == 1
    assert r["attempts"] >= 1


def test_intent_slot_cas_conflict(ak):
    ak_mod, Proxy = ak
    r1 = ak_mod.commit_rebased.intent_commit(
        Proxy, agent_id="orchestrator", note="first",
        expect_slot="task:TASK-X=v0",
    )
    assert r1["status"] == "committed"
    r2 = ak_mod.commit_rebased.intent_commit(
        Proxy, agent_id="orchestrator", note="stale",
        expect_slot="task:TASK-X=v0",
    )
    assert r2["status"] == "conflict"


def test_commit_with_rebase_pins_task_creation_and_workspace_revisions(ak):
    ak_mod, Proxy = ak
    task = {
        "contract_version": "2.0", "task_id": "TASK-MANAGED", "status": "proposed",
        "objective": "exercise automatic revision pins", "owner_run_id": "run-owner",
        "reviewer": "run-reviewer", "read_paths": [], "write_paths": [],
        "confidentiality": "private", "dependencies": [], "input_artifact_hashes": {},
        "output": {"path": "proof.json", "schema": "proof.v1"},
        "acceptance_commands": ["pytest"], "assumptions": [], "stop_conditions": [],
        "retry_budget": 1,
    }
    created = ak_mod.commit_rebased.commit_with_rebase(
        Proxy, "task.created", task, agent_id="orchestrator")
    assert created["status"] == "committed"

    context = {"session_id": "sess-managed", "status": "ACTIVE",
               "active_task_ids": ["TASK-MANAGED"], "evidence_refs": [], "next_steps": []}
    updated = ak_mod.commit_rebased.commit_with_rebase(
        Proxy, "workspace.context", context, agent_id="orchestrator")
    assert updated["status"] == "committed"


def test_compact_writes_snapshot_and_cursor(ak, cw, av):
    ak_mod, Proxy = ak
    for i in range(3):
        cw.commit("task.transition", {"task_id": "TC", "status": f"s{i}"})
    receipt = ak_mod.compact.compact(Proxy, force=True, agent_id="orchestrator")
    assert receipt["status"] == "compacted"
    assert receipt["epoch"] == 1
    snap = Path(av.ROOT) / receipt["path"]
    assert snap.exists()
    doc = json.loads(snap.read_text())
    assert doc["merkle_root"].startswith("sha256:")
    assert doc["last_seq"] == 3
    cursor = cw.read_compact_cursor()
    assert cursor is not None and cursor["last_seq"] == 3
    # Compaction moves FILES, it never shortens the chain: event_files() spans the live
    # directory and the archive, so a compacted journal still replays from seq 1. The old
    # assertion here (live prefix == delta) encoded the truncating behaviour that lost state.
    chain = cw.read_prefix()
    assert [e["seq"] for e in chain] == list(range(1, len(chain) + 1)), "compaction lost events"
    assert {e["seq"] for e in chain} >= {1, 2, 3}, "pre-snapshot events must remain replayable"
    assert any(e["type"] == "snapshot.created" for e in chain)

    # Only the delta stays in the live directory; everything through last_seq is archived.
    # Dotfiles (the compact cursor) are bookkeeping, not events — event_files() skips them too.
    live_files = {p.name for p in cw.commits_dir().glob("*.json") if not p.name.startswith(".")}
    archived_files = {p.name for p in cw.archive_dir().glob("*.json")}
    assert set(receipt["archived"]) == archived_files
    assert live_files.isdisjoint(archived_files)
    assert all(int(name.split("-", 1)[0]) > 3 for name in live_files)
    assert all(int(name.split("-", 1)[0]) <= 3 for name in archived_files)

    seq, h, problems = cw.verify_chain()
    assert not problems
    assert seq >= 4


def test_reclaimed_lease_after_snapshot_commit_blocks_all_compaction_caches(
        ak, cw, av, monkeypatch):
    ak_mod, Proxy = ak
    cw.commit("task.transition", {"task_id": "TC-STALE", "status": "open"})
    real_commit = ak_mod.compact.commit_rebased.commit_with_rebase

    def commit_then_reclaim(*args, **kwargs):
        receipt = real_commit(*args, **kwargs)
        for lease_path in av.LEASES.glob("*.json"):
            lease = json.loads(lease_path.read_text())
            if lease.get("resources") == ["MemoryBank/**"]:
                lease["lease_token"] = "reclaimed-generation"
                av.atomic_write_json(lease_path, lease)
        return receipt

    monkeypatch.setattr(ak_mod.compact.commit_rebased, "commit_with_rebase",
                        commit_then_reclaim)
    receipt = ak_mod.compact.compact(Proxy, force=True, agent_id="orchestrator")
    assert receipt["status"] == "error" and "lease token/fence" in receipt["reason"]
    assert not list((av.COORD / "snapshots").glob("state_v*.json"))
    assert not (av.COORD / "commits" / ".compact_cursor.json").exists()
    assert not list((av.COORD / "commits" / "archive").glob("*.json"))


def test_hydrate_bounded(ak, cw):
    ak_mod, Proxy = ak
    cw.commit("task.transition", {"task_id": "HY", "status": "open"})
    out = ak_mod.hydrate.hydrate(Proxy, task_id="HY", budget=400)
    assert out["status"] == "ok"
    assert out["token_estimate"] <= 400
    assert "HY" in out["digest"]


def test_fingerprint_promote_and_reject(ak, av):
    ak_mod, Proxy = ak
    root = av.ROOT / "MemoryBank" / "fingerprint"
    cand_dir = root / "candidate"
    cand_dir.mkdir(parents=True, exist_ok=True)
    good = {
        "schema_version": "1.0",
        "id": "FP-TEST-GOOD",
        "stage": "candidate",
        "rule": {
            "subject": "tests",
            "predicate": "tests must pass before done",
            "description": "gate exit 0 required",
        },
        "assertions": ["gate"],
        "fixtures": [
            {"name": "ok", "setup": {}, "expect": {"ok": True}},
            {"name": "has_gate", "setup": {}, "expect": {"contains": "gate"}},
        ],
        "tags": ["test"],
    }
    good_path = cand_dir / "FP-TEST-GOOD.json"
    good_path.write_text(json.dumps(good, indent=2))
    promoted = ak_mod.fingerprint_eval.promote_or_reject(Proxy, good_path)
    assert promoted["status"] == "approved"
    assert (root / "approved" / "FP-TEST-GOOD.json").exists()

    bad = {
        "schema_version": "1.0",
        "id": "FP-TEST-BAD",
        "stage": "candidate",
        "rule": {"description": "no fixtures will fail schema"},
        "assertions": [],
        "fixtures": [],
        "tags": [],
    }
    bad_path = cand_dir / "FP-TEST-BAD.json"
    bad_path.write_text(json.dumps(bad, indent=2))
    rejected = ak_mod.fingerprint_eval.promote_or_reject(Proxy, bad_path)
    assert rejected["status"] == "rejected"

    # Contradiction against approved
    contra = {
        "schema_version": "1.0",
        "id": "FP-TEST-CONTRA",
        "stage": "candidate",
        "rule": {
            "subject": "tests",
            "predicate": "not tests must pass before done",
            "description": "flip",
        },
        "assertions": ["x"],
        "fixtures": [{"name": "ok", "setup": {}, "expect": {"ok": True}}],
        "tags": [],
    }
    contra_path = cand_dir / "FP-TEST-CONTRA.json"
    contra_path.write_text(json.dumps(contra, indent=2))
    r = ak_mod.fingerprint_eval.promote_or_reject(Proxy, contra_path)
    assert r["status"] == "rejected"
    assert any("contradiction" in c for c in r["report"]["conflicts"])


def test_reservation_ttl(ak):
    ak_mod, Proxy = ak
    a = ak_mod.reservations.acquire(Proxy, agent_id="orchestrator", resource="scripts/foo", ttl_sec=60)
    assert a["status"] == "ok"
    b = ak_mod.reservations.acquire(Proxy, agent_id="code_generator", resource="scripts/foo", ttl_sec=60)
    assert b["status"] == "conflict"
    ak_mod.reservations.release(Proxy, a["token"])
    c = ak_mod.reservations.acquire(Proxy, agent_id="code_generator", resource="scripts/foo", ttl_sec=60)
    assert c["status"] == "ok"
