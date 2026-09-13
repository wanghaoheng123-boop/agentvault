"""RFC T11 — freshness/state, plus the mail correctness fixes that landed with P02."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from conftest import TZ, claim, ns


# ---------------------------------------------------------------- frontmatter scoping

def test_frontmatter_field_is_scoped_to_the_block(av):
    text = (
        "---\nsession_id: real-session\nlast_updated: 2026-09-06T21:00:00+08:00\n---\n\n"
        "# Body\n\nsession_id: spoofed-by-prose\n"
    )
    assert av.frontmatter_field(text, "session_id") == "real-session"


def test_frontmatter_field_returns_none_without_frontmatter(av):
    assert av.frontmatter_field("# No frontmatter\nsession_id: nope\n", "session_id") is None


# ------------------------------------------------------- no prose-driven state inference

def test_prose_complete_does_not_change_thread_state(av):
    """Any backticked COMPLETE anywhere used to flip the session and close COORD-001."""
    threads = {
        "schema_version": "1.0",
        "threads": [
            {"id": "COORD-001", "title": "Coordination", "owner": "orchestrator", "status": "active"},
            {"id": "SESS-sess-test", "title": "Test", "owner": "orchestrator", "status": "active"},
        ],
    }
    (av.COORD / "threads.json").write_text(json.dumps(threads, indent=2))
    fresh = (datetime.now(TZ) - timedelta(hours=1)).isoformat(timespec="seconds")
    av.CURRENT.write_text(
        f"---\nversion: 1.0\nlast_updated: {fresh}\nsession_id: sess-test\n"
        "stale_after_hours: 48\n---\n\n"
        "# CURRENT\n\n"
        "Do not mark the task `COMPLETE` until peer review lands.\n"
        "- **SubTask:** **COMPLETE** was the old trigger phrase\n"
    )
    assert av.cmd_refresh(ns(session="", agent="orchestrator", views_only=True, run=None)) == 0
    after = json.loads((av.COORD / "threads.json").read_text())
    assert [t["status"] for t in after["threads"]] == ["active", "active"]
    assert after == threads, "refresh must not mutate thread state at all"


# -------------------------------------------------------------- refresh lease semantics

def test_refresh_views_only_never_touches_current(av):
    before = av.CURRENT.read_bytes()
    assert av.cmd_refresh(ns(session="", agent="orchestrator", views_only=True, run=None)) == 0
    assert av.CURRENT.read_bytes() == before
    assert av.BOARD.exists() and av.ACTIVE.exists()
    assert "generated_at:" in av.BOARD.read_text()


def test_refresh_without_lease_is_views_only_and_exits_2(av):
    before = av.CURRENT.read_bytes()
    rc = av.cmd_refresh(ns(session="", agent="orchestrator", views_only=False, run=None))
    assert rc == 2
    assert av.CURRENT.read_bytes() == before
    assert av.BOARD.exists(), "views are still regenerated"


def test_refresh_with_lease_updates_current(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    before = av.CURRENT.read_bytes()
    assert av.cmd_refresh(ns(session="", agent="orchestrator", views_only=False, run=None)) == 0
    assert av.CURRENT.read_bytes() != before


def test_refresh_cannot_rejuvenate_via_a_descendant_lease(av):
    """A lease on something *under* MemoryBank must not authorize writing CURRENT.md."""
    assert claim(av, "orchestrator", "MemoryBank/coord/next_ids.json", ttl="10m") == 0
    before = av.CURRENT.read_bytes()
    assert av.cmd_refresh(ns(session="", agent="orchestrator", views_only=False, run=None)) == 2
    assert av.CURRENT.read_bytes() == before


# ------------------------------------------------------------- generated vs verified

def test_verify_requires_a_lease(av):
    assert av.cmd_verify(ns(agent="orchestrator", run=None, note="checked")) == 1


def test_verify_stamps_last_verified_at(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    assert av.cmd_verify(ns(agent="orchestrator", run=None, note="checked the board")) == 0
    text = av.CURRENT.read_text()
    assert av.frontmatter_field(text, "last_verified_at") is not None


def test_refresh_does_not_advance_last_verified_at(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    av.cmd_verify(ns(agent="orchestrator", run=None, note="first"))
    verified = av.frontmatter_field(av.CURRENT.read_text(), "last_verified_at")
    assert av.cmd_refresh(ns(session="", agent="orchestrator", views_only=False, run=None)) == 0
    assert av.frontmatter_field(av.CURRENT.read_text(), "last_verified_at") == verified


# ------------------------------------------------------------------------- mail semantics

def test_ack_prefix_is_rejected_when_ambiguous(av):
    base = av.MAIL / "orchestrator"
    for name in ("MSG-1.json", "MSG-100001.json"):
        (base / "cur" / name).write_text(json.dumps({"id": name[:-5], "status": "pending"}))
    # Exact match wins even though a longer prefix-sibling exists.
    assert av.cmd_ack(ns(agent="orchestrator", msg="MSG-1")) == 0
    assert (base / "done" / "MSG-1.json").exists()
    assert (base / "cur" / "MSG-100001.json").exists(), "must not ack the wrong message"


def test_ack_ambiguous_prefix_without_exact_match_fails(av):
    base = av.MAIL / "orchestrator"
    for name in ("MSG-10001.json", "MSG-10002.json"):
        (base / "cur" / name).write_text(json.dumps({"id": name[:-5], "status": "pending"}))
    assert av.cmd_ack(ns(agent="orchestrator", msg="MSG-100")) == 1
    assert len(list((base / "cur").glob("*.json"))) == 2


def test_audit_appends_are_not_interleaved(av):
    """Concurrent audit writers must produce whole lines, never torn ones."""
    import threading

    def writer(n):
        for i in range(50):
            av.audit("concurrent_test", worker=n, i=i, pad="x" * 400)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [l for l in av.AUDIT.read_text().splitlines() if l.strip()]
    assert len(lines) == 200
    for line in lines:
        json.loads(line)  # every line must be complete, parseable JSON


# ------------------------------------------------------- P03 envelope: resume / idempotency / hops

def _post(av, **kw):
    base = dict(from_agent="orchestrator", to_agent="code_generator", type="assign",
                summary="s", refs="", task_id="", hop=0, parent=None, intent="",
                idempotency_key="", run=None)
    base.update(kw)
    return av.cmd_post(ns(**base))


def test_recv_resume_surfaces_unfinished_cur(av, capsys):
    """A crash between recv and acting on a message used to hide it from every view."""
    _post(av)
    assert av.cmd_recv(ns(agent="code_generator", full=False, resume=False)) == 0
    capsys.readouterr()

    # Plain recv finds nothing new; the message is stranded in cur/.
    av.cmd_recv(ns(agent="code_generator", full=False, resume=False))
    plain = json.loads(capsys.readouterr().out)
    assert plain["moved_to_cur"] == [] and plain["messages"] == []

    av.cmd_recv(ns(agent="code_generator", full=False, resume=True))
    resumed = json.loads(capsys.readouterr().out)
    assert len(resumed["resumed_from_cur"]) == 1
    assert resumed["messages"][0]["summary"] == "s"


def test_duplicate_post_with_idempotency_key_makes_one_message(av, capsys):
    _post(av, idempotency_key="job-1", summary="first")
    first = json.loads(capsys.readouterr().out)
    _post(av, idempotency_key="job-1", summary="second attempt")
    second = json.loads(capsys.readouterr().out)
    assert second["id"] == first["id"]
    assert second["summary"] == "first", "the original effect is returned, not a new one"
    assert len(list((av.MAIL / "code_generator" / "new").glob("*.json"))) == 1


def test_hop_is_derived_from_parent_not_from_the_caller(av, capsys):
    _post(av, summary="root")
    root = json.loads(capsys.readouterr().out)
    assert root["hop"] == 0
    # Caller lies about --hop; the server must ignore it and use parent.hop + 1.
    _post(av, parent=root["id"], hop=0, summary="child")
    child = json.loads(capsys.readouterr().out)
    assert child["hop"] == 1 and child["parent_id"] == root["id"]


def test_hop_cap_cannot_be_reset_through_a_parent_chain(av, capsys):
    _post(av, summary="root")
    prev = json.loads(capsys.readouterr().out)
    for expected in range(1, 8):
        rc = _post(av, parent=prev["id"], hop=0, summary=f"h{expected}")
        out = capsys.readouterr().out
        if rc != 0:
            break
        prev = json.loads(out)
        assert prev["hop"] == expected
    # At hop 8 a non-terminal message must be refused however the caller sets --hop.
    assert _post(av, parent=prev["id"], hop=0, type="assign", summary="over") == 1


def test_refs_carry_content_hashes(av, capsys):
    _post(av, refs="AGENTS.md")
    msg = json.loads(capsys.readouterr().out)
    assert msg["refs"][0]["path"] == "AGENTS.md"
    assert len(msg["refs"][0]["sha256"]) == 64


def test_missing_parent_is_rejected(av):
    assert _post(av, parent="MSG-DOES-NOT-EXIST") == 1


def test_duplicate_ack_returns_one_immutable_effect_receipt(av, capsys):
    _post(av, summary="ack once")
    message = json.loads(capsys.readouterr().out)
    assert av.cmd_recv(ns(agent="code_generator", full=False, resume=False)) == 0
    capsys.readouterr()
    assert av.cmd_ack(ns(agent="code_generator", msg=message["id"])) == 0
    first = json.loads(capsys.readouterr().out)
    receipt_path = av.MAIL / "code_generator" / "receipts" / f"{message['id']}.json"
    before = receipt_path.read_bytes()

    assert av.cmd_ack(ns(agent="code_generator", msg=message["id"])) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "duplicate"
    assert second["receipt"] == first["receipt"]
    assert receipt_path.read_bytes() == before
    assert len(list(receipt_path.parent.glob(f"{message['id']}*.json"))) == 1


def test_ack_recovers_after_message_reached_done_before_receipt(av, capsys):
    _post(av, summary="crash boundary")
    message = json.loads(capsys.readouterr().out)
    assert av.cmd_recv(ns(agent="code_generator", full=False, resume=False)) == 0
    capsys.readouterr()
    src = av.MAIL / "code_generator" / "cur" / f"{message['id']}.json"
    data = json.loads(src.read_text())
    data.update(status="acked", acked_at="2026-09-10T00:00:00+08:00")
    src.write_text(json.dumps(data, indent=2) + "\n")
    done = av.MAIL / "code_generator" / "done" / src.name
    os.replace(src, done)

    assert av.cmd_ack(ns(agent="code_generator", msg=message["id"])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "duplicate"
    assert result["receipt"]["acked_at"] == "2026-09-10T00:00:00+08:00"
    assert (av.MAIL / "code_generator" / "receipts" / src.name).is_file()
