"""AK-SPWS remediation regressions — audit findings A and B (REVIEW-STRUCTURE-20260908).

Acceptance this file encodes, verbatim from the review's T01 line:
  "One winning CAS receipt; no acknowledged orphan writes; replay matches state;
   repeated compaction preserves owner/evidence/version fields."

Each test states the invariant it defends, not the behaviour it happened to observe. The
interleavings are driven deterministically rather than by threads, so a failure here is a
real defect and never a scheduling flake.
"""
from __future__ import annotations

import json

import pytest

from test_akspws import ak, cw  # noqa: F401  (shared kernel/journal fixtures)


# --------------------------------------------------------------- A: one winning CAS receipt

def test_only_one_writer_can_win_a_slot_version(ak, monkeypatch):
    """A losing compare-and-swap must never be acknowledged as `ok`.

    The failure this guards (audit finding A): the conditional UPDATE affects no row, but the
    winner has already advanced the slot to exactly the version the loser intended to write.
    Re-reading and comparing against that number therefore *confirms the winner's write* and
    hands the loser a success receipt for a value that was never stored.

    The stale read is injected rather than raced, so the interleaving is exact and repeatable.
    """
    ak_mod, Proxy = ak
    store = ak_mod.store
    conn = store.connect(Proxy)
    store.cas_slot(conn, "task:race", "task", {"writer": "initial"}, 0, "t0")

    stale = store.get_slot(conn, "task:race")
    assert stale["version"] == 1

    winner = store.cas_slot(conn, "task:race", "task", {"writer": "winner"}, 1, "t1")
    assert winner["status"] == "ok" and winner["version"] == 2

    # The loser read the slot before the winner committed, and only now issues its UPDATE.
    real_get_slot, calls = store.get_slot, {"n": 0}

    def stale_first(connection, slot_id):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_get_slot(connection, slot_id)

    monkeypatch.setattr(store, "get_slot", stale_first)
    loser = store.cas_slot(conn, "task:race", "task", {"writer": "loser"}, 1, "t2")
    monkeypatch.undo()

    assert loser["status"] != "ok", "a write that changed no row was acknowledged as success"
    assert loser["status"] == "conflict"

    stored = store.get_slot(conn, "task:race")
    conn.close()
    assert stored["value"] == {"writer": "winner"}, "the stored value must be the winner's"
    assert stored["version"] == 2, "a lost update must not advance the version"


def test_a_successful_cas_still_reports_the_version_it_wrote(ak):
    """Guard the fix from over-correcting: genuine sequential writes must keep succeeding."""
    ak_mod, Proxy = ak
    store = ak_mod.store
    conn = store.connect(Proxy)
    assert store.cas_slot(conn, "task:seq", "task", {"n": 0}, 0, "t0")["version"] == 1
    for expected_version in (1, 2, 3):
        receipt = store.cas_slot(conn, "task:seq", "task", {"n": expected_version},
                                 expected_version, f"t{expected_version}")
        assert receipt["status"] == "ok"
        assert receipt["version"] == expected_version + 1
        assert store.get_slot(conn, "task:seq")["value"] == {"n": expected_version}
    assert store.cas_slot(conn, "task:seq", "task", {"n": 99}, 1, "t9")["status"] == "conflict"
    conn.close()


# ------------------------------------------------------ B: no acknowledged orphan writes

def test_a_rejected_journal_append_leaves_no_slot_behind(ak, cw):
    """SQLite is a projection, so a refused journal write must leave no durable slot.

    The failure this guards (audit finding B): the slot moved before the journal accepted the
    event, so a rejected append left a committed slot change with no corresponding event.
    """
    ak_mod, Proxy = ak
    store = ak_mod.store

    # Corrupt the journal so any append is refused.
    cw.ensure_dirs()
    (cw.commits_dir() / "00000001-broken.json").write_text("{", encoding="utf-8")

    result = ak_mod.commit_rebased.intent_commit(
        Proxy, agent_id="orchestrator", note="fault injection", expect_slot="task:orphan=v0")
    assert result["status"] != "committed"

    conn = store.connect(Proxy)
    orphan = store.get_slot(conn, store.slot_key("task", "orphan"))
    conn.close()
    assert orphan is None, "a refused journal append acknowledged a durable slot write"


# ----------------------------------------------------------------- replay matches state

def test_rebuilt_projections_equal_a_pure_fold_of_the_journal(cw):
    """Projections are derived: rebuilding from events must reproduce them exactly."""
    cw.commit("task.transition", {"task_id": "R1", "status": "open", "owner": "agent-a"})
    cw.commit("task.transition", {"task_id": "R2", "status": "open"})
    cw.commit("task.transition", {"task_id": "R1", "status": "done"})

    cw.rebuild_projections()
    folded = cw.fold(cw.read_prefix())["tasks"]
    for task_id, expected in folded.items():
        on_disk = json.loads((cw.projections_dir() / "tasks" / f"{task_id}.json").read_text())
        assert on_disk == expected, f"projection for {task_id} diverged from the journal fold"
    assert set(folded) == {"R1", "R2"}
    assert folded["R1"]["revision"] == 2 and folded["R1"]["owner"] == "agent-a"


# ------------------------------------------- repeated compaction preserves task fields

def test_a_partial_update_after_compaction_keeps_earlier_fields(ak, cw):
    """A later event carrying only `status` must not erase owner/evidence set before a snapshot.

    Compaction moves event *files*; it must not shorten the chain the fold reads.
    """
    ak_mod, Proxy = ak
    cw.commit("task.transition", {"task_id": "T1", "status": "open",
                                  "owner": "agent-a", "evidence_refs": ["proof1"]})
    assert ak_mod.compact.compact(Proxy, force=True, agent_id="orchestrator")["status"] == "compacted"
    cw.commit("task.transition", {"task_id": "T1", "status": "done"})

    projected = json.loads((cw.projections_dir() / "tasks" / "T1.json").read_text())
    assert projected["status"] == "done"
    assert projected["owner"] == "agent-a", "compaction dropped a field set before the snapshot"
    assert projected["evidence_refs"] == ["proof1"]
    assert projected["revision"] == 2, "revision must count every transition, across snapshots"


def test_repeated_compaction_is_idempotent_for_unchanged_state(ak, cw):
    """Compacting twice with no intervening change must not lose tasks from the snapshot."""
    ak_mod, Proxy = ak
    cw.commit("task.transition", {"task_id": "T1", "status": "open", "owner": "agent-a"})
    cw.commit("task.transition", {"task_id": "T2", "status": "open"})

    second = ak_mod.compact.compact(Proxy, force=True, agent_id="orchestrator")
    third = ak_mod.compact.compact(Proxy, force=True, agent_id="orchestrator")
    assert second["status"] == "compacted" and third["status"] == "compacted"

    snap2 = json.loads((Proxy.ROOT / second["path"]).read_text())
    snap3 = json.loads((Proxy.ROOT / third["path"]).read_text())
    assert set(snap2["folded"]["tasks"]) == {"T1", "T2"}
    assert set(snap3["folded"]["tasks"]) == set(snap2["folded"]["tasks"]), \
        "a later snapshot dropped tasks absent from the live delta"
    assert snap3["folded"]["tasks"]["T1"]["owner"] == "agent-a"

    seq, _, problems = cw.verify_chain()
    assert not problems and seq >= 4
