"""RFC T10 — control journal: hash chain, idempotency, prefix validity, crash recovery."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import SRC, expected_head


@pytest.fixture
def cw(av, writer_credentials):
    spec = importlib.util.spec_from_file_location("cw_under_test", SRC / "commit_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cw_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.av.configure_paths(av.ROOT)
    original = mod.commit
    def authenticated_commit(event_type, body, **kw):
        kw.setdefault("expected", expected_head(mod, event_type, body, kw.get("task_id", "")))
        return original(event_type, body, **kw)
    mod.commit = authenticated_commit
    return mod


# ------------------------------------------------------------------------------ basics

def test_empty_journal_is_a_valid_empty_prefix(cw):
    assert cw.verify_chain() == (0, cw.ZERO_HASH, [])
    assert cw.read_prefix() == []


def test_commit_chains_and_projects(cw):
    r1 = cw.commit("task.transition", {"task_id": "TASK-A", "status": "open"})
    r2 = cw.commit("task.transition", {"task_id": "TASK-A", "status": "in_progress"})
    assert (r1["seq"], r2["seq"]) == (1, 2)

    events = cw.read_prefix()
    assert events[0]["prev_hash"] == cw.ZERO_HASH
    assert events[1]["prev_hash"] == events[0]["hash"]

    task = json.loads((cw.projections_dir() / "tasks" / "TASK-A.json").read_text())
    assert task["status"] == "in_progress"
    assert task["revision"] == 2
    meta = json.loads((cw.projections_dir() / "_meta.json").read_text())
    assert meta["journal_seq"] == 2 and meta["authority"] == "legacy"


def test_payload_blobs_match_their_recorded_hash(cw):
    cw.commit("artifact.registered",
              {"artifact_id": "ART-1", "canonical_path": "reports/x.md", "owner": "doc_writer"},
              payloads=[{"provenance": "note", "unicode": "研究"}])
    seq, _, problems = cw.verify_chain()
    assert (seq, problems) == (1, [])


def test_idempotency_key_returns_the_existing_event(cw):
    a = cw.commit("task.transition", {"task_id": "T", "status": "open"}, idempotency_key="k1")
    assert a["status"] == "committed"

    # Replaying the SAME request returns the original receipt and repeats no effect.
    b = cw.commit("task.transition", {"task_id": "T", "status": "open"}, idempotency_key="k1")
    assert b["status"] == "duplicate" and b["seq"] == a["seq"]
    assert len(cw.read_prefix()) == 1

    # A DIFFERENT request under the same key is a collision, not a silent overwrite. Without
    # this branch the key could be ignored entirely and the test above would still pass.
    with pytest.raises(ValueError, match="reused for a different request"):
        cw.commit("task.transition", {"task_id": "T", "status": "done"}, idempotency_key="k1")
    assert len(cw.read_prefix()) == 1
    assert cw.read_prefix()[0]["body"]["status"] == "open"


def test_resources_are_canonicalized_and_escapes_rejected(cw):
    r = cw.commit("task.transition", {"task_id": "T", "status": "open"},
                  resources=[".//scripts//coord/**"])
    assert cw.read_prefix()[0]["resources"] == ["scripts/coord"]
    with pytest.raises(cw.av.CanonError):
        cw.commit("task.transition", {"task_id": "T2", "status": "open"},
                  resources=["../../etc/passwd"])


def test_unknown_event_type_is_refused(cw):
    with pytest.raises(ValueError):
        cw.commit("totally.made.up", {})


# ------------------------------------------------------------- prefix validity / tamper

def test_tampered_event_truncates_the_valid_prefix(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    cw.commit("task.transition", {"task_id": "T", "status": "in_progress"})
    cw.commit("task.transition", {"task_id": "T", "status": "done"})

    victim = sorted(cw.commits_dir().glob("00000002-*.json"))[0]
    d = json.loads(victim.read_text())
    d["body"]["status"] = "TAMPERED"
    victim.write_text(json.dumps(d, indent=2))

    seq, _, problems = cw.verify_chain()
    assert seq == 1, "consumers must not read past the first invalid event"
    assert "hash mismatch" in problems[0]
    assert len(cw.read_prefix()) == 1


def test_gap_in_sequence_truncates_the_prefix(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    cw.commit("task.transition", {"task_id": "T", "status": "two"})
    cw.commit("task.transition", {"task_id": "T", "status": "three"})
    sorted(cw.commits_dir().glob("00000002-*.json"))[0].unlink()
    seq, _, problems = cw.verify_chain()
    assert seq == 1 and problems


def test_missing_payload_invalidates_its_event(cw):
    cw.commit("artifact.registered", {"artifact_id": "A"}, payloads=[{"x": 1}])
    next(cw.payloads_dir().glob("*.json")).unlink()
    seq, _, problems = cw.verify_chain()
    assert seq == 0 and "missing payload" in problems[0]


def test_commit_refuses_to_append_to_a_broken_chain(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    victim = sorted(cw.commits_dir().glob("00000001-*.json"))[0]
    d = json.loads(victim.read_text())
    d["body"]["status"] = "TAMPERED"
    victim.write_text(json.dumps(d, indent=2))
    with pytest.raises(RuntimeError, match="not clean"):
        cw.commit("task.transition", {"task_id": "T", "status": "next"})


# ----------------------------------------------------------------------- crash recovery

def test_replay_quarantines_never_deletes(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    cw.commit("task.transition", {"task_id": "T", "status": "two"})
    victim = sorted(cw.commits_dir().glob("00000002-*.json"))[0]
    d = json.loads(victim.read_text())
    d["body"]["status"] = "TAMPERED"
    victim.write_text(json.dumps(d, indent=2))

    plan = cw.replay()
    assert plan["valid_prefix_seq"] == 1
    assert victim.name in plan["quarantined"]
    assert not victim.exists()
    # Each incident gets its own directory, so quarantining the same event name twice cannot
    # overwrite earlier evidence.
    incidents = [d for d in cw.quarantine_dir().iterdir() if d.is_dir()]
    assert len(incidents) == 1
    moved = incidents[0] / victim.name
    assert moved.exists(), "quarantined, not deleted"
    assert json.loads(moved.read_text())["body"]["status"] == "TAMPERED"
    assert (incidents[0] / "reason.txt").exists()


def test_replay_dry_run_mutates_nothing(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    victim = sorted(cw.commits_dir().glob("00000001-*.json"))[0]
    d = json.loads(victim.read_text())
    d["body"]["status"] = "TAMPERED"
    victim.write_text(json.dumps(d, indent=2))
    before = victim.read_bytes()

    plan = cw.replay(dry_run=True)
    assert plan["status"] == "dry_run"
    assert victim.exists() and victim.read_bytes() == before
    assert list(cw.quarantine_dir().glob("*.json")) == []


def test_rebuild_projections_is_idempotent(cw):
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    first = cw.rebuild_projections()
    second = cw.rebuild_projections()
    assert first["journal_seq"] == second["journal_seq"] == 1
    assert first["journal_hash"] == second["journal_hash"]


def test_orphan_payload_from_a_crash_has_no_authority(cw):
    """A crash between payload write and event publication leaves a blob nothing points at."""
    cw.ensure_dirs()
    orphan = cw.payloads_dir() / ("a" * 64 + ".json")
    orphan.write_text('{"orphan": true}')
    seq, _, problems = cw.verify_chain()
    assert (seq, problems) == (0, []), "an unreferenced payload must not affect the chain"
    assert orphan.exists(), "and must not be deleted"


def test_crash_after_payload_fsync_before_event_publication_has_no_authority(cw, monkeypatch):
    body = {"artifact_id": "ART-CRASH", "canonical_path": "reports/crash.md", "owner": "doc_writer"}
    with monkeypatch.context() as scoped:
        scoped.setattr(cw.os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError, match="crash"):
            cw.commit("artifact.registered", body, payloads=[{"evidence": "durable-before-event"}],
                      idempotency_key="crash-before-publication")
    seq, _, problems = cw.verify_chain()
    assert (seq, problems) == (0, [])
    assert len(list(cw.payloads_dir().glob("*.json"))) == 1
    assert len(list(cw.staging_dir().glob("*.json"))) == 1


def test_duplicate_retry_repairs_projection_after_post_commit_crash(cw, monkeypatch):
    body = {"task_id": "TASK-POST-COMMIT-CRASH", "status": "open"}
    with monkeypatch.context() as scoped:
        scoped.setattr(cw, "rebuild_projections",
                       lambda *args, **kwargs: (_ for _ in ()).throw(OSError("projection crash")))
        with pytest.raises(OSError, match="projection crash"):
            cw.commit("task.transition", body, idempotency_key="crash-after-publication")
    assert cw.verify_chain()[0] == 1
    assert not (cw.projections_dir() / "tasks/TASK-POST-COMMIT-CRASH.json").exists()

    duplicate = cw.commit("task.transition", body, idempotency_key="crash-after-publication")
    assert duplicate["status"] == "duplicate" and duplicate["seq"] == 1
    projected = json.loads((cw.projections_dir() / "tasks/TASK-POST-COMMIT-CRASH.json").read_text())
    assert projected["status"] == "open" and projected["revision"] == 1


def test_duplicate_sequence_publication_is_refused(cw):
    """os.link gives atomicity AND exclusivity: a second writer at the same seq gets EEXIST."""
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    existing = sorted(cw.commits_dir().glob("00000001-*.json"))[0]
    clash = cw.commits_dir() / "00000002-deadbeefcafe.json"
    clash.write_text(existing.read_text())
    # seq 2 is occupied by an invalid event, so the chain is dirty and append is refused.
    with pytest.raises(RuntimeError):
        cw.commit("task.transition", {"task_id": "T", "status": "two"})


# --------------------------------------------------------------------- staging boundary

def test_journal_writes_nothing_outside_its_own_tree(cw, av):
    current_before = av.CURRENT.read_bytes()
    threads_before = (av.COORD / "threads.json").read_bytes()
    cw.commit("journal.import", {"threads": [{"id": "COORD-001", "status": "active"}]})
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    assert av.CURRENT.read_bytes() == current_before, "P03 must not touch CURRENT.md"
    assert (av.COORD / "threads.json").read_bytes() == threads_before, "P03 must not touch threads.json"
    assert not av.BOARD.exists()  # board is only written by refresh
    # The staged projection is separate from the legacy file.
    assert (cw.projections_dir() / "threads.json").exists()


def test_authority_stays_legacy(cw):
    assert cw.read_protocol()["authority"] == "legacy"
    cw.commit("task.transition", {"task_id": "T", "status": "open"})
    meta = json.loads((cw.projections_dir() / "_meta.json").read_text())
    assert meta["authority"] == "legacy"
