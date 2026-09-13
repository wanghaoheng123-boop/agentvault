"""CLI coverage for journal tasks, CURRENT projection, and authority maintenance."""

from __future__ import annotations

import contextlib
import io
import json
from contextlib import contextmanager


def invoke(av, *argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = av.main(list(argv))
    return rc, stdout.getvalue(), stderr.getvalue()


def contract(task_id="TASK-CLI"):
    return {
        "contract_version": "2.0", "task_id": task_id, "status": "proposed",
        "objective": "exercise the managed task CLI", "owner_run_id": "run-owner",
        "reviewer": "orchestrator", "read_paths": ["inputs/**"],
        "write_paths": ["outputs/result.json"], "confidentiality": "private",
        "dependencies": [], "input_artifact_hashes": {},
        "output": {"path": "outputs/result.json", "schema": "result.v1"},
        "acceptance_commands": ["bin/avcoord gate"], "assumptions": [],
        "stop_conditions": ["retry budget exhausted"], "retry_budget": 1,
    }


def seed_journal(av):
    writer = av.journal_worker()
    seq, digest, _ = writer.verify_chain()
    writer.commit("journal.import", {"threads": []},
                  expected={"journal_seq": seq, "journal_hash": digest})
    seq, digest, _ = writer.verify_chain()
    writer.commit("workspace.context", {
        "session_id": "sess-cutover", "status": "ACTIVE", "active_task_ids": [],
        "evidence_refs": ["proof://legacy-import"], "next_steps": ["complete cutover"],
    }, expected={"journal_seq": seq, "journal_hash": digest, "workspace_revision": 0})


def activate_journal(av, run, rfc="RFC-TEST"):
    seed_journal(av)
    rc, _, error = invoke(av, "cutover", "--to", "journal", "--rfc", rfc,
                          "--agent", "orchestrator", "--run", run)
    assert rc == 0, error


def test_task_and_workspace_commands_commit_then_project(av, writer_credentials, tmp_path, clock):
    run = writer_credentials["run_id"]
    activate_journal(av, run)
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(contract()))
    rc, output, _ = invoke(av, "task", "create", "--file", str(task_file),
                           "--agent", "orchestrator", "--run", run)
    assert rc == 0 and json.loads(output)["status"] == "committed"

    for status in ("ready", "claimed", "in_progress", "review"):
        rc, _, error = invoke(av, "task", "transition", "--task-id", "TASK-CLI",
                              "--status", status, "--agent", "orchestrator", "--run", run)
        assert rc == 0, error
    rc, _, _ = invoke(av, "task", "transition", "--task-id", "TASK-CLI",
                      "--status", "verified", "--agent", "orchestrator", "--run", run)
    assert rc == 1
    rc, output, error = invoke(
        av, "task", "review", "--task-id", "TASK-CLI", "--decision", "ACK",
        "--evidence-ref", "proof://gate/1", "--agent", "orchestrator", "--run", run)
    assert rc == 0, error
    review_hash = json.loads(output)["hash"]
    rc, _, error = invoke(
        av, "task", "transition", "--task-id", "TASK-CLI", "--status", "verified",
        "--agent", "orchestrator", "--run", run, "--evidence-ref", "proof://gate/1",
        "--reviewer-receipt", review_hash)
    assert rc == 0, error

    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({
        "session_id": "sess-cli", "status": "ACTIVE", "active_task_ids": ["TASK-CLI"],
        "evidence_refs": ["proof://gate/1"], "next_steps": ["integrate after review"],
    }))
    rc, _, error = invoke(av, "workspace", "set", "--file", str(context_file),
                          "--agent", "orchestrator", "--run", run)
    assert rc == 0, error
    assert "session_id: sess-cli" in av.CURRENT.read_text()

    before = av.CURRENT.read_bytes()
    clock.advance(minutes=1)
    rc, _, _ = invoke(av, "refresh", "--views-only")
    assert rc == 0 and av.CURRENT.read_bytes() == before
    rc, output, error = invoke(av, "verify", "--agent", "orchestrator", "--run", run,
                               "--note", "checked exact task and gate receipts")
    assert rc == 0, error
    assert json.loads(output)["journal"]["status"] == "committed"
    assert av.CURRENT.read_bytes() != before

    rc, output, _ = invoke(av, "task", "show", "--task-id", "TASK-CLI")
    assert rc == 0 and json.loads(output)["status"] == "verified"


def test_cutover_is_write_free_in_preview_and_rollback_exports_full_journal(
        av, writer_credentials, tmp_path):
    run = writer_credentials["run_id"]
    seed_journal(av)
    migration = av.COORD / "migrations" / "RFC-TEST"
    rc, output, error = invoke(av, "cutover", "--to", "journal", "--rfc", "RFC-TEST",
                               "--agent", "orchestrator", "--run", run, "--dry-run")
    assert rc == 0, error
    assert json.loads(output)["status"] == "dry_run" and not migration.exists()

    rc, _, error = invoke(av, "cutover", "--to", "journal", "--rfc", "RFC-TEST",
                          "--agent", "orchestrator", "--run", run)
    assert rc == 0, error
    assert av.read_protocol()["authority"] == "journal"
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(contract("TASK-CUTOVER")))
    assert invoke(av, "task", "create", "--file", str(task_file),
                  "--agent", "orchestrator", "--run", run)[0] == 0

    rc, output, error = invoke(av, "cutover", "--to", "legacy", "--rfc", "RFC-TEST",
                               "--agent", "orchestrator", "--run", run)
    assert rc == 0, error
    result = json.loads(output)
    export = av.ROOT / result["recovery_artifact"]
    document = json.loads(export.read_text())
    assert document["kind"] == "post-cutover-export"
    assert document["events"] and document["folded_state"]["tasks"]["TASK-CUTOVER"]
    assert av.read_protocol()["authority"] == "legacy"


def test_force_cutover_is_always_rejected(av, writer_credentials):
    run = writer_credentials["run_id"]
    rc, _, error = invoke(av, "cutover", "--to", "journal", "--rfc", "RFC-TEST",
                          "--agent", "orchestrator", "--run", run, "--force")
    assert rc == 1 and "disabled" in error


def test_idempotent_journal_cutover_repairs_current_after_protocol_flip_crash(
        av, writer_credentials):
    run = writer_credentials["run_id"]
    activate_journal(av, run, "RFC-CRASH-REPAIR")
    (av.CURRENT).write_text("STALE-CURRENT\n")
    rc, output, error = invoke(av, "cutover", "--to", "journal", "--rfc", "RFC-CRASH-REPAIR",
                               "--agent", "orchestrator", "--run", run)
    assert rc == 0, error
    assert "repaired and verified" in output
    current = av.CURRENT.read_text()
    assert "journal_seq:" in current and "journal_hash: sha256:" in current


def test_cutover_revalidates_lease_before_writing_recovery(
        av, writer_credentials, monkeypatch):
    run = writer_credentials["run_id"]
    activate_journal(av, run, "RFC-AUTH-SEED")
    writer = av.journal_worker()

    def deny(*args, **kwargs):
        raise PermissionError("synthetic stale lease proof")

    monkeypatch.setattr(writer, "authorize", deny)
    monkeypatch.setattr(av, "journal_worker", lambda: writer)
    rc, _, error = invoke(av, "cutover", "--to", "legacy", "--rfc", "RFC-AUTH-FAIL",
                          "--agent", "orchestrator", "--run", run)
    assert rc == 1 and "stale lease proof" in error
    assert not (av.COORD / "migrations/RFC-AUTH-FAIL").exists()


def test_cutover_rechecks_workspace_drain_inside_coord_lock(
        av, writer_credentials, monkeypatch):
    run = writer_credentials["run_id"]
    activate_journal(av, run, "RFC-DRAIN-SEED")
    original_lock = av.coord_lock
    injected = {"done": False}

    @contextmanager
    def lock_with_late_claim():
        with original_lock():
            if not injected["done"]:
                injected["done"] = True
                late = {
                    "schema_version": "1.1", "agent_id": "code_generator", "run_id": "",
                    "fence": 999, "run_fence": 999, "resources": ["scripts/late/**"],
                    "resource_keys": ["scripts/late"], "lease_token": "late-claim",
                    "reason": "deterministic cutover race", "task_id": "",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                    "renewed_at": None, "created_at": "2026-09-10T00:00:00+00:00",
                }
                av.atomic_write_json(av.LEASES / "late-claim.json", late)
            yield

    monkeypatch.setattr(av, "coord_lock", lock_with_late_claim)
    rc, _, error = invoke(av, "cutover", "--to", "legacy", "--rfc", "RFC-DRAIN-RACE",
                          "--agent", "orchestrator", "--run", run)
    assert rc == 1 and "acquired during cutover" in error
    assert av.read_protocol()["authority"] == "journal"
    assert not (av.COORD / "migrations/RFC-DRAIN-RACE").exists()


def test_journal_refresh_revalidates_fence_before_current_write(
        av, writer_credentials, monkeypatch):
    run = writer_credentials["run_id"]
    activate_journal(av, run, "RFC-REFRESH-AUTH")
    before = av.CURRENT.read_bytes()
    writer = av.journal_worker()

    def deny(**kwargs):
        raise PermissionError("synthetic reclaimed fence")

    monkeypatch.setattr(writer, "guarded_rebuild_projections", deny)
    monkeypatch.setattr(av, "journal_worker", lambda: writer)
    rc, _, error = invoke(av, "refresh", "--agent", "orchestrator", "--run", run)
    assert rc == 2 and "reclaimed fence" in error
    assert av.CURRENT.read_bytes() == before


def test_doctor_rejects_nonjournal_current_under_journal_authority(
        av, writer_credentials, capsys):
    run = writer_credentials["run_id"]
    activate_journal(av, run, "RFC-DOCTOR-CURRENT")
    av.CURRENT.write_text("legacy pointer\n")
    assert av.cmd_doctor(type("Args", (), {})()) == 1
    assert "JOURNAL_CURRENT_INVALID" in capsys.readouterr().out
