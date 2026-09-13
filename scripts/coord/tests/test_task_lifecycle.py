"""Structured task lifecycle and evidence gates (structure review T06)."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from conftest import SRC, expected_head


@pytest.fixture
def cw(av, writer_credentials):
    spec = importlib.util.spec_from_file_location("cw_task_lifecycle", SRC / "commit_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cw_task_lifecycle"] = mod
    spec.loader.exec_module(mod)
    mod.av.configure_paths(av.ROOT)
    return mod


def contract(task_id="TASK-NEW"):
    return {
        "contract_version": "2.0",
        "task_id": task_id,
        "status": "proposed",
        "objective": "Prove the lifecycle contract",
        "owner_run_id": "run-owner",
        "reviewer": "orchestrator",
        "read_paths": ["inputs/**"],
        "write_paths": ["outputs/result.json"],
        "confidentiality": "private",
        "dependencies": [],
        "input_artifact_hashes": {"input": "sha256:" + "1" * 64},
        "output": {"path": "outputs/result.json", "schema": "result.v1"},
        "acceptance_commands": ["python3 -m pytest tests/test_result.py"],
        "assumptions": [],
        "stop_conditions": ["retry budget exhausted"],
        "retry_budget": 2,
    }


def append(cw, event_type, body):
    expected = expected_head(cw, event_type, body)
    if event_type == "task.created":
        expected["task_revision"] = {body["task_id"]: 0}
    elif event_type == "review.receipt":
        expected["task_revision"] = {
            body["task_id"]: cw.fold(cw.read_prefix())["tasks"][body["task_id"]]["revision"]
        }
    return cw.commit(event_type, body, expected=expected)


def test_task_contract_requires_the_complete_reviewable_shape(cw):
    body = contract()
    del body["acceptance_commands"]
    with pytest.raises(ValueError, match="acceptance_commands"):
        append(cw, "task.created", body)


def test_owner_cannot_be_its_own_reviewer(cw):
    body = contract()
    body["reviewer"] = body["owner_run_id"]
    with pytest.raises(ValueError, match="different principals"):
        append(cw, "task.created", body)


@pytest.mark.parametrize("field", ["owner_run_id", "reviewer"])
def test_task_principals_must_be_nonempty_safe_ids(cw, field):
    body = contract()
    body[field] = ""
    with pytest.raises(ValueError, match=field):
        append(cw, "task.created", body)


def test_task_cannot_skip_lifecycle_states(cw):
    body = contract()
    append(cw, "task.created", body)
    with pytest.raises(RuntimeError, match="invalid task transition"):
        append(cw, "task.transition", {"task_id": body["task_id"], "status": "integrated"})


def test_verification_and_integration_require_evidence(cw):
    body = contract()
    tid = body["task_id"]
    append(cw, "task.created", body)
    for state in ("ready", "claimed", "in_progress", "review"):
        append(cw, "task.transition", {"task_id": tid, "status": state})
    with pytest.raises(RuntimeError, match="verified requires"):
        append(cw, "task.transition", {"task_id": tid, "status": "verified"})
    with pytest.raises(RuntimeError, match="committed review.receipt"):
        append(cw, "task.transition", {"task_id": tid, "status": "verified",
                                        "evidence_refs": ["proof://gate/1"],
                                        "reviewer_receipt": "sha256:" + "0" * 64})
    current = cw.fold(cw.read_prefix())["tasks"][tid]
    review = append(cw, "review.receipt", {
        "task_id": tid, "task_revision": current["revision"],
        "task_state_sha256": cw.task_state_sha256(current), "decision": "ACK",
        "evidence_refs": ["proof://gate/1"],
    })
    with pytest.raises(RuntimeError, match="not an independent ACK"):
        append(cw, "task.transition", {"task_id": tid, "status": "verified",
                                        "evidence_refs": ["proof://different"],
                                        "reviewer_receipt": review["hash"]})
    append(cw, "task.transition", {"task_id": tid, "status": "verified",
                                    "evidence_refs": ["proof://gate/1"],
                                    "reviewer_receipt": review["hash"]})
    with pytest.raises(RuntimeError, match="integrated requires"):
        append(cw, "task.transition", {"task_id": tid, "status": "integrated"})
    append(cw, "task.transition", {"task_id": tid, "status": "integrated",
                                    "integration_evidence": ["proof://combined-gate/1"]})
    task = cw.fold(cw.read_prefix())["tasks"][tid]
    assert task["status"] == "integrated"
    assert task["revision"] == 7


def test_block_review_receipt_cannot_verify(cw):
    body = contract("TASK-BLOCK-REVIEW")
    append(cw, "task.created", body)
    for state in ("ready", "claimed", "in_progress", "review"):
        append(cw, "task.transition", {"task_id": body["task_id"], "status": state})
    current = cw.fold(cw.read_prefix())["tasks"][body["task_id"]]
    review = append(cw, "review.receipt", {
        "task_id": body["task_id"], "task_revision": current["revision"],
        "task_state_sha256": cw.task_state_sha256(current), "decision": "BLOCK",
        "evidence_refs": ["proof://blocker/1"],
    })
    with pytest.raises(RuntimeError, match="not an independent ACK"):
        append(cw, "task.transition", {"task_id": body["task_id"], "status": "verified",
                                        "evidence_refs": ["proof://blocker/1"],
                                        "reviewer_receipt": review["hash"]})


def test_blocked_task_has_one_explicit_resume_route(cw):
    body = contract()
    tid = body["task_id"]
    append(cw, "task.created", body)
    append(cw, "task.transition", {"task_id": tid, "status": "blocked",
                                    "blockers": ["dependency unavailable"]})
    append(cw, "task.transition", {"task_id": tid, "status": "ready"})
    assert cw.fold(cw.read_prefix())["tasks"][tid]["status"] == "ready"


def workspace_expected(cw):
    expected = expected_head(cw)
    expected["workspace_revision"] = cw.fold(cw.read_prefix())["workspace"]["revision"]
    return expected


def test_current_is_a_journal_projection_and_refresh_time_is_not_verification(cw, av, clock):
    (av.COORD / "protocol.json").write_text(
        '{"schema_version":"1.0","epoch":1,"authority":"journal"}\n')
    context = {"session_id": "sess-managed", "status": "ACTIVE — implementation review",
               "active_task_ids": [], "evidence_refs": ["proof://baseline"],
               "next_steps": ["Run the independent review"]}
    cw.commit("workspace.context", context, expected=workspace_expected(cw))
    first = av.CURRENT.read_text()
    assert "session_id: sess-managed" in first
    assert "proof://baseline" in first
    assert "last_verified_at: null" in first
    assert cw.fold(cw.read_prefix())["workspace"]["last_verified_at"] is None

    cw.commit("workspace.verified", {"note": "checked task and evidence revisions"},
              expected=workspace_expected(cw))
    verified = cw.fold(cw.read_prefix())["workspace"]["last_verified_at"]
    clock.advance(minutes=10)
    cw.rebuild_projections(write_current=True)
    redrawn = av.CURRENT.read_text()
    assert f"last_verified_at: {verified}" in redrawn
    assert redrawn != first


def test_workspace_update_requires_exact_revision(cw):
    context = {"session_id": "sess-managed", "status": "ACTIVE", "active_task_ids": [],
               "evidence_refs": [], "next_steps": []}
    expected = workspace_expected(cw)
    expected["workspace_revision"] = 9
    with pytest.raises(RuntimeError, match="workspace_revision"):
        cw.commit("workspace.context", context, expected=expected)


def test_non_workspace_commit_does_not_rewrite_contested_current(cw, av):
    (av.COORD / "protocol.json").write_text(
        '{"schema_version":"1.0","epoch":1,"authority":"journal"}\n')
    context = {"session_id": "sess-managed", "status": "ACTIVE", "active_task_ids": [],
               "evidence_refs": [], "next_steps": []}
    cw.commit("workspace.context", context, expected=workspace_expected(cw))
    before = av.CURRENT.read_bytes()
    append(cw, "task.created", contract("TASK-NO-CURRENT-WRITE"))
    assert av.CURRENT.read_bytes() == before


def test_sequential_intent_slot_versions_use_canonical_receipt_id(cw):
    first = {"status": "ok", "id": "task:TASK-X", "kind": "task", "version": 1,
             "value": {"status": "ready"}}
    cw.commit("intent.recorded", {"note": "first", "slot": first},
              expected={**expected_head(cw), "slots": {"task:TASK-X": 0}})
    second = {**first, "version": 2, "value": {"status": "claimed"}}
    receipt = cw.commit("intent.recorded", {"note": "second", "slot": second},
                        expected={**expected_head(cw), "slots": {"task:TASK-X": 1}})
    assert receipt["status"] == "committed"


def test_authority_change_requires_exact_epoch_and_revision(cw):
    recovery = cw.av.ROOT / "MemoryBank/coord/migrations/test/export.json"
    recovery.parent.mkdir(parents=True, exist_ok=True)
    recovery.write_text('{"export":"complete"}\n')
    body = {"from_authority": "legacy", "to_authority": "journal",
            "epoch_from": 1, "epoch_to": 2,
            "recovery_artifact": "MemoryBank/coord/migrations/test/export.json",
            "recovery_artifact_sha256": cw.regular_file_sha256(recovery)}
    expected = {**expected_head(cw), "authority_revision": 0}
    receipt = cw.commit("authority.changed", body, expected=expected)
    assert receipt["status"] == "committed"
    authority = cw.fold(cw.read_prefix())["authority"]
    assert authority["current"] == "journal" and authority["epoch"] == 2

    invalid = {**body, "from_authority": "journal", "to_authority": "legacy",
               "epoch_from": 2, "epoch_to": 4}
    with pytest.raises(ValueError, match="exactly one"):
        cw.commit("authority.changed", invalid,
                  expected={**expected_head(cw), "authority_revision": 1})
