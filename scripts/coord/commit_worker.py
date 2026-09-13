#!/usr/bin/env python3
"""AgentVault control journal — the single protected writer.

The journal is authoritative when `MemoryBank/coord/protocol.json` selects the
``journal`` authority. Durable events are folded into disposable projections,
including ``MemoryBank/CURRENT.md``; callers never update those views as state.

Journal shape
-------------
    MemoryBank/coord/
      protocol.json                     {"epoch": 1, "authority": "legacy"}
      commits/
        .staging/{uuid}.json            pre-publication scratch
        00000001-3f9a2c7b1d4e.json      {seq:08d}-{hash12}.json
        payloads/{sha256}.json          content-addressed immutable blobs
        _quarantine/                    events after the first invalid one; moved, never deleted
      projections/                      generated views; rebuildable from the journal alone
        tasks/{task_id}.json
        artifacts/index.json
        threads.json
        _meta.json                      {journal_seq, journal_hash, generated_at}

Publication uses os.link(), which is atomic *and* fails EEXIST if the sequence number is
already taken — giving atomicity and exclusivity in one operation. os.replace would
silently overwrite a duplicate seq; O_CREAT|O_EXCL on the final name would forfeit atomic
publication.

Consumers accept only a complete valid prefix: read events in sequence order and stop at
the first gap, chain mismatch, schema failure or missing payload.

CLI:
    python3 scripts/coord/commit_worker.py verify
    python3 scripts/coord/commit_worker.py head
    python3 scripts/coord/commit_worker.py commit --file event.json
    python3 scripts/coord/commit_worker.py replay [--dry-run]
    python3 scripts/coord/commit_worker.py rebuild
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import hmac
import stat
from contextlib import nullcontext
import sys
import uuid
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location("_avcoord_lib", Path(__file__).resolve().parent / "avcoord.py")
av = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(av)

SCHEMA_VERSION = "2.0"
ZERO_HASH = "sha256:" + "0" * 64

EVENT_TYPES = {
    "task.created",
    "task.transition",
    "review.receipt",
    "workspace.context",
    "workspace.verified",
    "authority.changed",
    "artifact.registered",
    "lease.acquired",
    "lease.released",
    "mail.receipt",
    "journal.import",
    "library.admit",
    # AK-SPWS (ADR-009)
    "intent.recorded",
    "snapshot.created",
    "fingerprint.promoted",
    "fingerprint.rejected",
}

REQUIRED_FIELDS = ("schema_version", "seq", "prev_hash", "epoch", "ts", "type", "body")

TASK_CONTRACT_FIELDS = {
    "task_id",
    "objective",
    "owner_run_id",
    "reviewer",
    "read_paths",
    "write_paths",
    "confidentiality",
    "dependencies",
    "input_artifact_hashes",
    "output",
    "acceptance_commands",
    "assumptions",
    "stop_conditions",
    "retry_budget",
}
TASK_STATES = {
    "proposed",
    "ready",
    "claimed",
    "in_progress",
    "review",
    "verified",
    "integrated",
    "blocked",
    "cancelled",
}
TASK_TRANSITIONS = {
    "proposed": {"ready", "blocked", "cancelled"},
    "ready": {"claimed", "blocked", "cancelled"},
    "claimed": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"review", "blocked", "cancelled"},
    "review": {"verified", "in_progress", "blocked", "cancelled"},
    "verified": {"integrated", "in_progress", "blocked"},
    "blocked": {"ready", "cancelled"},
    "integrated": set(),
    "cancelled": set(),
}


# ------------------------------------------------------------------------------- paths


def commits_dir() -> Path:
    return av.COORD / "commits"


def payloads_dir() -> Path:
    return commits_dir() / "payloads"


def staging_dir() -> Path:
    return commits_dir() / ".staging"


def quarantine_dir() -> Path:
    return commits_dir() / "_quarantine"


def archive_dir() -> Path:
    return commits_dir() / "archive"


def compact_cursor_path() -> Path:
    """Pointer written by avkernel.compact after archiving a prefix."""
    return commits_dir() / ".compact_cursor.json"


def projections_dir() -> Path:
    return av.COORD / "projections"


def read_compact_cursor() -> dict | None:
    p = compact_cursor_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_compact_cursor(cursor: dict) -> None:
    ensure_dirs()
    av.atomic_write_json(compact_cursor_path(), cursor)


def protocol_path() -> Path:
    return av.COORD / "protocol.json"


def ensure_dirs() -> None:
    for d in (commits_dir(), payloads_dir(), staging_dir(), quarantine_dir(), archive_dir(),
              projections_dir(), projections_dir() / "tasks", projections_dir() / "artifacts"):
        secure_path(d).mkdir(parents=True, exist_ok=True)


def read_protocol() -> dict:
    return av.load_json(protocol_path(), {"schema_version": "1.0", "epoch": 1, "authority": "legacy"})


# -------------------------------------------------------------------------------- hashing


def canonical_bytes(event: dict) -> bytes:
    """Pinned canonical form. Two implementations must agree byte-for-byte or the chain
    diverges silently, so this is deliberately explicit rather than 'whatever json does'."""
    body = {k: v for k, v in event.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_hash(event: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(event)).hexdigest()


def payload_bytes(data: Any) -> bytes:
    """The exact bytes a payload blob is stored as.

    Hash and file must agree byte-for-byte: hashing a compact form while writing an
    indented one makes every payload fail its own integrity check.
    """
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def payload_hash(data: Any) -> str:
    return hashlib.sha256(payload_bytes(data)).hexdigest()


def task_state_sha256(task: dict) -> str:
    raw = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def regular_file_sha256(path: Path) -> str:
    """Hash one stable regular file without following any path symlink."""
    path = secure_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("recovery artifact must be a regular file")
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        leaf = os.stat(path, follow_symlinks=False)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(leaf):
            raise ValueError("recovery artifact changed while it was hashed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------ reading


def event_files() -> list[tuple[int, Path]]:
    """Full live AND archived journal. Cursors and snapshots confer no authority."""
    out = []
    for directory in (commits_dir(), archive_dir()):
        if directory.exists():
            for p in directory.glob("*.json"):
                if p.name.startswith("."):
                    continue
                prefix = p.name.split("-", 1)[0]
                out.append((int(prefix) if prefix.isdigit() else -1, p))
    return sorted(out, key=lambda pair: (pair[0], str(pair[1])))


def safe_id(value: Any, label: str = "identifier") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", value):
        raise ValueError(f"invalid {label}: expected a bounded plain identifier")
    return value


def secure_path(path: Path) -> Path:
    """Protected output paths never follow symlinks, including an existing leaf."""
    path = path.absolute()
    root = av.ROOT.resolve()
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("protected path escapes workspace") from None
    current = root
    for part in parts:
        if part in (".", ".."):
            raise ValueError("non-canonical protected path")
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink in protected path: {current}")
    return path


def validate_body(event_type: str, body: Any, task_id: str = "") -> None:
    if not isinstance(body, dict):
        raise ValueError("event body must be an object")
    if task_id:
        safe_id(task_id, "task_id")
    for key in ("task_id", "artifact_id", "factor_id"):
        if key in body and body[key]:
            safe_id(body[key], key)
    if event_type in {"task.created", "task.transition"} and not (body.get("task_id") or task_id):
        raise ValueError(f"{event_type} requires task_id")
    if event_type == "task.created":
        missing = sorted(TASK_CONTRACT_FIELDS - set(body))
        if missing:
            raise ValueError(f"task.created missing contract fields: {', '.join(missing)}")
        if body.get("status") != "proposed" or body.get("contract_version") != "2.0":
            raise ValueError("task.created requires contract_version=2.0 and status=proposed")
        for key in ("read_paths", "write_paths", "dependencies", "acceptance_commands",
                    "assumptions", "stop_conditions"):
            if not isinstance(body[key], list):
                raise ValueError(f"task.created {key} must be a list")
        if not isinstance(body["retry_budget"], int) or body["retry_budget"] < 0:
            raise ValueError("task.created retry_budget must be a non-negative integer")
        safe_id(body.get("owner_run_id"), "owner_run_id")
        safe_id(body.get("reviewer"), "reviewer")
        if body["reviewer"] == body["owner_run_id"]:
            raise ValueError("task owner and reviewer must be different principals")
        if not isinstance(body["input_artifact_hashes"], dict):
            raise ValueError("task.created input_artifact_hashes must be an object")
        for digest in body["input_artifact_hashes"].values():
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("task input hashes must use sha256:<64 lowercase hex>")
    if event_type == "task.transition":
        status = body.get("status")
        if not isinstance(status, str):
            raise ValueError("task.transition requires a string status")
    if event_type == "review.receipt":
        required = {"task_id", "task_revision", "task_state_sha256", "decision", "evidence_refs"}
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"review.receipt missing fields: {', '.join(missing)}")
        if type(body["task_revision"]) is not int or body["task_revision"] < 1:
            raise ValueError("review.receipt task_revision must be a positive integer")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", body["task_state_sha256"]):
            raise ValueError("review.receipt requires a task_state_sha256")
        if body["decision"] not in {"ACK", "BLOCK"}:
            raise ValueError("review.receipt decision must be ACK or BLOCK")
        if (not isinstance(body["evidence_refs"], list) or not body["evidence_refs"]
                or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 2048
                       for ref in body["evidence_refs"])):
            raise ValueError("review.receipt requires bounded evidence_refs")
    if event_type == "workspace.context":
        required = {"session_id", "status", "active_task_ids", "evidence_refs", "next_steps"}
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"workspace.context missing fields: {', '.join(missing)}")
        safe_id(body["session_id"], "session_id")
        for key in ("active_task_ids", "evidence_refs", "next_steps"):
            if not isinstance(body[key], list):
                raise ValueError(f"workspace.context {key} must be a list")
        for value in body["active_task_ids"]:
            safe_id(value, "active_task_id")
    if event_type == "workspace.verified":
        if not isinstance(body.get("note"), str) or not body["note"].strip():
            raise ValueError("workspace.verified requires a non-empty note")
    if event_type == "authority.changed":
        required = {"from_authority", "to_authority", "epoch_from", "epoch_to",
                    "recovery_artifact", "recovery_artifact_sha256"}
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"authority.changed missing fields: {', '.join(missing)}")
        if ({body["from_authority"], body["to_authority"]} != {"legacy", "journal"}
                or body["from_authority"] == body["to_authority"]):
            raise ValueError("authority.changed must transition between legacy and journal")
        if (type(body["epoch_from"]) is not int or type(body["epoch_to"]) is not int
                or body["epoch_to"] != body["epoch_from"] + 1):
            raise ValueError("authority.changed must advance the epoch by exactly one")
        recovery = av.canon_resource(body["recovery_artifact"])
        if not recovery.display or recovery.explicit_subtree:
            raise ValueError("authority.changed recovery artifact must name one workspace file")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", body["recovery_artifact_sha256"]):
            raise ValueError("authority.changed recovery artifact requires sha256:<64 lowercase hex>")
    for record in body.get("records", []):
        if not isinstance(record, dict):
            raise ValueError("artifact record must be an object")
        safe_id(record.get("artifact_id"), "artifact_id")
    for thread in body.get("threads", []):
        if not isinstance(thread, dict):
            raise ValueError("thread must be an object")
        safe_id(thread.get("id"), "thread_id")


def validate_event(event: dict, expected_seq: int, expected_prev: str) -> str | None:
    try:
        if not isinstance(event, dict):
            return "event must be an object"
        for f in REQUIRED_FIELDS:
            if f not in event:
                return f"missing field '{f}'"
        if event["schema_version"] not in ("1.0", SCHEMA_VERSION):
            return "unsupported schema_version"
        if event["type"] not in EVENT_TYPES:
            return "unknown event type"
        if type(event["seq"]) is not int or event["seq"] != expected_seq:
            return f"seq {event['seq']} != expected {expected_seq}"
        if type(event["epoch"]) is not int or event["epoch"] < 1:
            return "invalid epoch"
        if event["prev_hash"] != expected_prev:
            return "prev_hash mismatch"
        if event.get("hash") != event_hash(event):
            return "hash mismatch (event body was altered)"
        validate_body(event["type"], event["body"], event.get("task_id", ""))
        for ref in event.get("payload_refs") or []:
            digest = ref.get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                return "invalid payload hash"
            if ref.get("path") != f"commits/payloads/{digest}.json":
                return "payload path is not content-addressed"
            p = secure_path(payloads_dir() / f"{digest}.json")
            if not p.is_file():
                return f"missing payload {ref['path']}"
            if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                return f"payload hash mismatch {ref['path']}"
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as e:
        return f"invalid event: {e}"
    return None


def _scan() -> tuple[list[dict], list[str], list[Path]]:
    events, problems, bad = [], [], []
    grouped = {}
    for seq, path in event_files():
        if path.is_symlink() or not re.fullmatch(r"[0-9]{8}-[0-9a-f]{12}\.json", path.name):
            problems.append(f"{path.name}: invalid event filename or symlink")
            bad.append(path)
            continue
        grouped.setdefault(seq, []).append(path)
    prev, expected_seq, epoch, stopped = ZERO_HASH, 1, 1, False
    for seq, paths in sorted(grouped.items()):
        if stopped:
            bad.extend(paths)
            continue
        try:
            raw = paths[0].read_bytes()
            if any(p.read_bytes() != raw for p in paths[1:]):
                raise ValueError("divergent copies of one sequence")
            event = json.loads(raw)
            reason = validate_event(event, expected_seq, prev)
            if not reason and (seq != event["seq"] or paths[0].name != f"{event['seq']:08d}-{event['hash'][7:19]}.json"):
                reason = "filename does not match event sequence/hash"
            if not reason and event["epoch"] < epoch:
                reason = "epoch moved backwards"
            if reason:
                raise ValueError(reason)
        except (ValueError, OSError, TypeError, KeyError) as e:
            problems.append(f"{paths[0].name}: {e}")
            bad.extend(paths)
            stopped = True
            continue
        events.append(event)
        prev, expected_seq, epoch = event["hash"], seq + 1, event["epoch"]
    return events, problems, bad


def verify_chain() -> tuple[int, str, list[str]]:
    events, problems, _ = _scan()
    return (events[-1]["seq"], events[-1]["hash"], problems) if events else (0, ZERO_HASH, problems)


def read_prefix() -> list[dict]:
    return _scan()[0]


def check_expected_cas(expected: dict | None, head_seq: int, head_hash: str) -> str | None:
    if not isinstance(expected, dict) or not {"journal_seq", "journal_hash"} <= expected.keys():
        return "expected CAS failed: journal_seq and journal_hash are required"
    if type(expected["journal_seq"]) is not int or expected["journal_seq"] != head_seq:
        return "expected CAS failed: journal_seq mismatch"
    if expected["journal_hash"] != head_hash:
        return "expected CAS failed: journal_hash mismatch"
    allowed = {"journal_seq", "journal_hash", "task_revision", "library_revision",
               "workspace_revision", "authority_revision", "slots"}
    if set(expected) - allowed:
        return "expected CAS failed: unsupported revision key"
    state = fold(read_prefix())
    for tid, revision in expected.get("task_revision", {}).items():
        safe_id(tid, "task_id")
        if type(revision) is not int or state["tasks"].get(tid, {}).get("revision", 0) != revision:
            return "expected CAS failed: task_revision mismatch"
    if "library_revision" in expected and expected["library_revision"] != len(state["library"]):
        return "expected CAS failed: library_revision mismatch"
    if "workspace_revision" in expected and expected["workspace_revision"] != state["workspace"]["revision"]:
        return "expected CAS failed: workspace_revision mismatch"
    if "authority_revision" in expected and expected["authority_revision"] != state["authority"]["revision"]:
        return "expected CAS failed: authority_revision mismatch"
    # Intent slot expectations describe the version BEFORE this event.
    slots = {}
    for event in read_prefix():
        if event["type"] == "intent.recorded":
            slot = event["body"].get("slot") or {}
            sid = slot.get("id") or slot.get("slot_key") or slot.get("slot") or slot.get("slot_id")
            if sid:
                slots[sid] = slot.get("version", 0)
    for sid, version in expected.get("slots", {}).items():
        if slots.get(sid, 0) != version:
            return "expected CAS failed: slot revision mismatch"
    return None


def authorize(resources: list[str], agent_id: str, run_id: str = "", run_token: str = "",
              lease_proofs: list[dict] | None = None) -> str:
    """Validate exact live principal and lease generations while caller holds coord.lock."""
    run_id = run_id or os.environ.get("AVCOORD_RUN_ID", "")
    run_token = run_token or os.environ.get("AVCOORD_RUN_TOKEN", "")
    run = av.load_run(run_id)
    if (not run or run.get("agent_id") != agent_id or not run.get("token_sha256")
            or not isinstance(run_token, str) or not hmac.compare_digest(
                run["token_sha256"], hashlib.sha256(run_token.encode()).hexdigest())):
        raise PermissionError("registered run and valid run token required")
    if lease_proofs is None:
        lease_proofs = json.loads(os.environ.get("AVCOORD_LEASE_PROOFS_JSON", "[]"))
    if not isinstance(lease_proofs, list):
        raise PermissionError("lease proofs must be a list")
    live = av.read_leases()
    for resource in resources:
        target = av.canon_resource(resource)
        permitted = False
        for lease in live:
            if av.lease_expired(lease) or not av._same_principal(lease, agent_id, run_id):
                continue
            if not any(av.res_covers(held, target) for held in av.lease_resources(lease)):
                continue
            if any(isinstance(p, dict) and p.get("fence") == lease.get("fence")
                   and p.get("lease_token") == lease.get("lease_token")
                   and p.get("run_id") == run_id for p in lease_proofs):
                permitted = True
                break
        if not permitted:
            raise PermissionError(f"missing live lease token/fence for {resource}")
    return run_id


def required_resources(event_type: str, body: dict, task_id: str = "") -> list[str]:
    # Every accepted event republishes the full derived projection set and metadata.
    out = ["MemoryBank/coord/commits/**", "MemoryBank/coord/projections/**"]
    if event_type in {"task.created", "task.transition", "review.receipt"}:
        tid = safe_id(body.get("task_id") or task_id, "task_id")
        if event_type != "review.receipt":
            out.append(f"MemoryBank/coord/projections/tasks/{tid}.json")
    elif event_type == "artifact.registered":
        out.append("MemoryBank/coord/projections/artifacts/index.json")
    elif event_type in {"workspace.context", "workspace.verified"}:
        out.append("MemoryBank/CURRENT.md")
    elif event_type == "authority.changed":
        out.append("MemoryBank/coord/protocol.json")
        out.append("MemoryBank/CURRENT.md")
    return out


# ------------------------------------------------------------------------------ writing


def commit(
    event_type: str,
    body: dict,
    *,
    agent_id: str = "orchestrator",
    run_id: str = "",
    task_id: str = "",
    resources: list[str] | None = None,
    idempotency_key: str = "",
    parent_id: str | None = None,
    payloads: list[Any] | None = None,
    expected: dict | None = None,
    dry_run: bool = False,
    run_token: str = "",
    lease_proofs: list[dict] | None = None,
) -> dict:
    """Append one event. Returns a receipt.

    Ordering (RFC §7): validate -> write payloads and fsync -> stage event and fsync ->
    os.link to publish -> fsync commits dir (THE COMMIT POINT) -> update projections.
    A crash before the link leaves orphan payloads with no authority; a crash after it
    leaves stale projections that `replay` rebuilds.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    validate_body(event_type, body, task_id)
    if event_type == "library.admit":
        raise PermissionError("AEAP admission disabled: no certified isolation and independent admission verifier")

    canon_resources, canon_keys = [], []
    for r in resources or []:
        c = av.canon_resource(r)  # raises CanonError on escape / bad glob — fail closed
        canon_resources.append(c.display)
        canon_keys.append(c.key)

    # Dry-run performs identical validation but never creates directories or lock files.
    with nullcontext() if dry_run else av.coord_lock():
        run_id = authorize(required_resources(event_type, body, task_id) + canon_resources,
                           agent_id, run_id, run_token, lease_proofs)
        request = {"type": event_type, "body": body, "agent_id": agent_id, "run_id": run_id,
                   "task_id": task_id, "resources": canon_resources, "parent_id": parent_id,
                   "payloads": payloads or []}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        head_seq, head_hash, problems = verify_chain()
        if problems:
            raise RuntimeError(f"journal is not clean, refusing to append: {problems[0]}")

        if idempotency_key:
            for e in read_prefix():
                if e.get("idempotency_key") == idempotency_key:
                    if e.get("request_sha256") != request_hash:
                        raise ValueError("idempotency key reused for a different request")
                    # A prior caller may have crashed after the journal commit point and
                    # before projections were published. The authenticated duplicate retry
                    # repairs those derived views before returning its original receipt.
                    rebuild_projections(write_current=(
                        e.get("type") in {"workspace.context", "workspace.verified"}
                        and read_protocol().get("authority", "legacy") == "journal"
                    ))
                    return {"status": "duplicate", "seq": e["seq"], "hash": e["hash"],
                            "idempotency_key": idempotency_key}
        cas_fail = check_expected_cas(expected, head_seq, head_hash)
        if cas_fail:
            raise RuntimeError(cas_fail)
        if event_type in {"task.created", "task.transition"}:
            tid = body.get("task_id") or task_id
            if tid not in expected.get("task_revision", {}):
                raise RuntimeError("expected CAS failed: explicit task_revision required")
            current = fold(read_prefix())["tasks"].get(tid)
            if event_type == "task.created":
                if current is not None or expected["task_revision"][tid] != 0:
                    raise RuntimeError("task.created requires an unused task id at revision 0")
            elif current and current.get("contract_version") == "2.0":
                previous, target = current.get("status"), body.get("status")
                if target not in TASK_STATES or target not in TASK_TRANSITIONS.get(previous, set()):
                    raise RuntimeError(f"invalid task transition: {previous!r} -> {target!r}")
                if target == "verified":
                    if not body.get("evidence_refs") or not body.get("reviewer_receipt"):
                        raise RuntimeError("verified requires evidence_refs and reviewer_receipt")
                    review = next((event for event in read_prefix()
                                   if event.get("hash") == body["reviewer_receipt"]), None)
                    if review is None or review.get("type") != "review.receipt":
                        raise RuntimeError("verified requires a committed review.receipt hash")
                    review_body = review.get("body") or {}
                    reviewer = current.get("reviewer")
                    if (review_body.get("decision") != "ACK"
                            or review_body.get("task_id") != tid
                            or review_body.get("task_revision") != current.get("revision")
                            or review_body.get("task_state_sha256") != task_state_sha256(current)
                            or review_body.get("evidence_refs") != body.get("evidence_refs")
                            or reviewer not in {review.get("agent_id"), review.get("run_id")}
                            or review.get("run_id") == current.get("owner_run_id")):
                        raise RuntimeError("reviewer receipt is not an independent ACK bound to this task revision and evidence")
                if target == "integrated" and not body.get("integration_evidence"):
                    raise RuntimeError("integrated requires integration_evidence")
        if event_type == "review.receipt":
            tid = body["task_id"]
            if tid not in expected.get("task_revision", {}):
                raise RuntimeError("expected CAS failed: explicit task_revision required")
            current = fold(read_prefix())["tasks"].get(tid)
            if current is None or current.get("contract_version") != "2.0":
                raise RuntimeError("review.receipt requires a managed v2 task")
            if current.get("status") != "review" or body["task_revision"] != current.get("revision"):
                raise RuntimeError("review.receipt must bind the current review revision")
            if body["task_state_sha256"] != task_state_sha256(current):
                raise RuntimeError("review.receipt task state hash mismatch")
            reviewer = current.get("reviewer")
            if reviewer not in {agent_id, run_id}:
                raise PermissionError("review receipt issuer is not the task's declared reviewer")
            if run_id == current.get("owner_run_id"):
                raise PermissionError("task owner run cannot issue its review receipt")
        if event_type in {"workspace.context", "workspace.verified"}:
            if expected.get("workspace_revision") != fold(read_prefix())["workspace"]["revision"]:
                raise RuntimeError("expected CAS failed: explicit workspace_revision required")
        if event_type == "authority.changed":
            authority = fold(read_prefix())["authority"]
            if expected.get("authority_revision") != authority["revision"]:
                raise RuntimeError("expected CAS failed: explicit authority_revision required")
            if (body["from_authority"] != authority["current"]
                    or body["epoch_from"] != authority["epoch"]):
                raise RuntimeError("authority transition does not match the committed journal state")
            recovery = av.canon_resource(body["recovery_artifact"])
            try:
                actual_recovery_hash = regular_file_sha256(av.ROOT / recovery.display)
            except (OSError, ValueError) as error:
                raise RuntimeError(f"invalid authority recovery artifact: {error}") from error
            if actual_recovery_hash != body["recovery_artifact_sha256"]:
                raise RuntimeError("authority recovery artifact hash mismatch")
        if not dry_run:
            ensure_dirs()

        payload_refs = []
        staged_payloads: list[Path] = []
        for data in payloads or []:
            h = payload_hash(data)
            pp = secure_path(payloads_dir() / f"{h}.json")
            if not dry_run and not pp.exists():
                av.atomic_write_text(pp, payload_bytes(data).decode("utf-8"))
                staged_payloads.append(pp)
            payload_refs.append({"path": f"commits/payloads/{h}.json", "sha256": h})

        event = {
            "schema_version": SCHEMA_VERSION,
            "request_sha256": request_hash,
            "seq": head_seq + 1,
            "prev_hash": head_hash,
            "epoch": read_protocol().get("epoch", 1),
            "ts": av.iso(),
            "agent_id": agent_id,
            "run_id": run_id,
            "task_id": task_id,
            "type": event_type,
            "idempotency_key": idempotency_key,
            "parent_id": parent_id,
            "expected": expected or {},
            "resources": canon_resources,
            "resource_keys": canon_keys,
            "payload_refs": payload_refs,
            "body": body,
        }
        event["hash"] = event_hash(event)
        final = secure_path(commits_dir() / f"{event['seq']:08d}-{event['hash'][7:19]}.json")

        if dry_run:
            return {"status": "dry_run", "seq": event["seq"], "hash": event["hash"], "path": str(final)}

        if payloads_dir().exists():
            av.fsync_dir(payloads_dir())

        # Validate against the same rules a consumer will apply, BEFORE publishing. Better
        # to fail the caller than to leave an event the chain will reject forever after.
        self_check = validate_event(event, head_seq + 1, head_hash)
        if self_check:
            raise RuntimeError(f"refusing to publish an invalid event: {self_check}")

        staged = staging_dir() / f"{uuid.uuid4().hex}.json"
        av.atomic_write_json(staged, event)
        try:
            os.link(staged, final)  # atomic + EEXIST if this seq is already published
        except FileExistsError:
            staged.unlink(missing_ok=True)
            raise RuntimeError(f"sequence {event['seq']} already published — concurrent writer?")
        av.fsync_dir(commits_dir())  # <== COMMIT POINT
        staged.unlink(missing_ok=True)

        rebuild_projections(write_current=(
            event_type in {"workspace.context", "workspace.verified"}
            and read_protocol().get("authority", "legacy") == "journal"
        ))
        av.audit("journal_commit", seq=event["seq"], type=event_type, agent=agent_id, hash=event["hash"])
        return {"status": "committed", "seq": event["seq"], "hash": event["hash"], "path": str(final)}


# -------------------------------------------------------------------------- projections


def fold(events: list[dict]) -> dict:
    """Fold the valid prefix into projection state. Pure — no I/O."""
    tasks: dict[str, dict] = {}
    reviews: dict[str, dict] = {}
    artifacts: dict[str, dict] = {}
    threads: dict[str, dict] = {}
    library: dict[str, dict] = {}
    workspace: dict[str, Any] = {"revision": 0, "context": None, "last_verified_at": None,
                                 "verification_note": None}
    authority: dict[str, Any] = {"revision": 0, "current": "legacy", "epoch": 1,
                                 "journal_seq": 0}

    for e in events:
        b = e.get("body") or {}
        if (e["type"] == "authority.changed"
                or (e["type"] == "task.transition" and b.get("status") == "authority_cutover")):
            authority.update(revision=authority["revision"] + 1,
                             current=b.get("to_authority", authority["current"]),
                             epoch=b.get("epoch_to", e.get("epoch", authority["epoch"])),
                             journal_seq=e["seq"], updated_at=e["ts"],
                             recovery_artifact=(b.get("recovery_artifact")
                                                or b.get("pre_cutover_snapshot")))
        if e["type"] in {"task.created", "task.transition"}:
            tid = b.get("task_id") or e.get("task_id")
            if tid:
                safe_id(tid, "task_id")
                t = tasks.setdefault(tid, {"task_id": tid, "revision": 0, "evidence_refs": []})
                t.update({k: v for k, v in b.items() if k != "task_id"})
                t["revision"] += 1
                t["journal_seq"] = e["seq"]
                t["updated_at"] = e["ts"]
        elif e["type"] == "review.receipt":
            reviews[e["hash"]] = {**b, "event_hash": e["hash"], "agent_id": e.get("agent_id"),
                                    "run_id": e.get("run_id"), "journal_seq": e["seq"],
                                    "reviewed_at": e["ts"]}
        elif e["type"] == "artifact.registered":
            records = b.get("records", []) or ([b] if b.get("artifact_id") else [])
            for record in records:
                aid = safe_id(record.get("artifact_id"), "artifact_id")
                artifacts[aid] = {**record, "journal_seq": e["seq"], "registered_at": e["ts"]}
        elif e["type"] == "library.admit":
            fid = b.get("factor_id")
            if fid:
                library[fid] = {**b, "journal_seq": e["seq"], "admitted_at": e["ts"]}
        elif e["type"] == "journal.import":
            for t in b.get("threads") or []:
                if t.get("id"):
                    threads[t["id"]] = {**t, "journal_seq": e["seq"]}
        elif e["type"] == "workspace.context":
            workspace["revision"] += 1
            workspace["context"] = {**b, "updated_at": e["ts"], "journal_seq": e["seq"]}
        elif e["type"] == "workspace.verified":
            workspace["revision"] += 1
            workspace["last_verified_at"] = e["ts"]
            workspace["verification_note"] = b.get("note")
            workspace["verification_journal_seq"] = e["seq"]
    return {"tasks": tasks, "reviews": reviews, "artifacts": artifacts, "threads": threads, "library": library,
            "workspace": workspace, "authority": authority}


def render_current(state: dict, head_seq: int, head_hash: str, generated_at: str) -> str | None:
    workspace = state.get("workspace") or {}
    context = workspace.get("context")
    if not context:
        return None
    verified = workspace.get("last_verified_at") or "null"
    active = context.get("active_task_ids") or []
    evidence = context.get("evidence_refs") or []
    next_steps = context.get("next_steps") or []
    lines = [
        "---", "version: 2.0", "priority: P0", f"generated_at: {generated_at}",
        f"last_updated: {context['updated_at']}", f"last_verified_at: {verified}",
        f"session_id: {context['session_id']}", "stale_after_hours: 48",
        f"journal_seq: {head_seq}", f"journal_hash: {head_hash}", "type: current_pointer", "---",
        "# CURRENT", "", "## Session", f"- **session_id:** `{context['session_id']}`", "",
        "## Active tasks",
    ]
    lines.extend(f"- `{task}`" for task in active)
    if not active:
        lines.append("- None")
    lines.extend(["", "## Status", str(context["status"]), "", "## Evidence"])
    lines.extend(f"- `{ref}`" for ref in evidence)
    if not evidence:
        lines.append("- None recorded")
    lines.extend(["", "## Next"])
    lines.extend(f"- {step}" for step in next_steps)
    if not next_steps:
        lines.append("- No next step recorded")
    return "\n".join(lines) + "\n"


def rebuild_projections(*, write_current: bool = False) -> dict:
    """Rebuild from the full journal; CURRENT requires an authorized caller.

    Ordinary replay and cache repair leave the contested CURRENT view untouched. The
    guarded commit path opts in only for events whose authorization includes CURRENT.
    """
    with av.coord_lock():
        events, problems, _ = _scan()
        state = fold(events)
        # Validate every destination before the first write.
        tasks_dir = secure_path(projections_dir() / "tasks")
        outputs = {tasks_dir / f"{safe_id(tid)}.json": task for tid, task in state["tasks"].items()}
        for path in outputs:
            secure_path(path)
        for name in ("artifacts/index.json", "library.json", "threads.json", "_meta.json"):
            secure_path(projections_dir() / name)
        ensure_dirs()
        head_seq = events[-1]["seq"] if events else 0
        head_hash = events[-1]["hash"] if events else ZERO_HASH
        stamp = av.iso()
        common = {"schema_version": "2.0", "journal_seq": head_seq, "journal_hash": head_hash,
                  "generated_at": stamp}
        outputs[projections_dir() / "artifacts/index.json"] = {**common, "artifacts": list(state["artifacts"].values())}
        outputs[projections_dir() / "library.json"] = {**common, "factors": list(state["library"].values())}
        outputs[projections_dir() / "threads.json"] = {**common, "threads": list(state["threads"].values())}
        for path, value in outputs.items():
            av.atomic_write_json(path, value)
        # Retire stale derived files, preserving bytes outside the active tasks view.
        for path in tasks_dir.glob("*.json"):
            if path not in outputs:
                secure_path(path)
                retired = secure_path(projections_dir() / "_retired" / f"{uuid.uuid4().hex}-{path.name}")
                retired.parent.mkdir(parents=True, exist_ok=True)
                os.rename(path, retired)
                av.fsync_dir(retired.parent)
        av.fsync_dir(tasks_dir)
        hashes = {str(path.relative_to(projections_dir())): hashlib.sha256(path.read_bytes()).hexdigest() for path in outputs}
        current = render_current(state, head_seq, head_hash, stamp)
        current_hash = None
        if current is not None and write_current:
            secure_path(av.CURRENT)
            av.atomic_write_text(av.CURRENT, current)
            current_hash = hashlib.sha256(av.CURRENT.read_bytes()).hexdigest()
        elif av.CURRENT.is_file() and not av.CURRENT.is_symlink():
            secure_path(av.CURRENT)
            # Carry the PREVIOUS recorded hash forward; do NOT re-hash what is on
            # disk. Re-hashing launders tampering: edit CURRENT.md's body, and the
            # next non-workspace journal commit (task.created, intent.recorded,
            # artifact.registered — all of which rebuild with write_current=False)
            # would adopt the tampered bytes as the new expected value. doctor
            # reported JOURNAL_CURRENT_TAMPERED once, then PASS forever after.
            # Only a real re-projection (write_current=True) may set a new hash.
            prior = av.load_json(projections_dir() / "_meta.json", {}) or {}
            current_hash = prior.get("current_sha256")
            if current_hash is None:
                # No prior record to preserve (first build): adopt what is there.
                current_hash = hashlib.sha256(av.CURRENT.read_bytes()).hexdigest()
        meta = {**common, "event_count": len(events), "authority": read_protocol().get("authority", "legacy"),
                "committed_authority": state["authority"],
                "projection_hashes": hashes, "current_sha256": current_hash,
                "journal_problems": problems}
        av.atomic_write_json(projections_dir() / "_meta.json", meta)
        av.fsync_dir(projections_dir())
        return meta


def guarded_rebuild_projections(*, agent_id: str, run_id: str, run_token: str,
                                lease_proofs: list[dict], write_current: bool = False) -> dict:
    """Authenticate live lease generations under the writer lock, then rebuild."""
    resources = ["MemoryBank/coord/projections/**"]
    if write_current:
        resources.append("MemoryBank/CURRENT.md")
    with av.coord_lock():
        authorize(resources, agent_id, run_id, run_token, lease_proofs)
        return rebuild_projections(write_current=write_current)


def replay(dry_run: bool = False, *, agent_id: str = "orchestrator", run_id: str = "",
           run_token: str = "", lease_proofs: list[dict] | None = None) -> dict:
    """Quarantine corrupt physical files without overwriting prior evidence."""
    with nullcontext() if dry_run else av.coord_lock():
        events, problems, bad = _scan()
        valid_seq = events[-1]["seq"] if events else 0
        head_hash = events[-1]["hash"] if events else ZERO_HASH
        plan = {"valid_prefix_seq": valid_seq, "head_hash": head_hash, "problems": problems,
                "would_quarantine": [str(p.relative_to(commits_dir())) for p in bad]}
        if dry_run:
            return {**plan, "status": "dry_run"}
        authorize(["MemoryBank/coord/commits/**", "MemoryBank/coord/projections/**"],
                  agent_id, run_id, run_token, lease_proofs)
        ensure_dirs()
        for path in bad:
            # A unique directory also preserves same-name live/archive evidence.
            dest_dir = secure_path(quarantine_dir() / uuid.uuid4().hex)
            dest_dir.mkdir()
            os.rename(path, dest_dir / path.name)
            av.atomic_write_text(dest_dir / "reason.txt", "\n".join(problems))
            av.fsync_dir(dest_dir)
            av.fsync_dir(path.parent)
        av.fsync_dir(quarantine_dir())
        return {**plan, "quarantined": plan["would_quarantine"], "meta": rebuild_projections(), "status": "replayed"}


# --------------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    av.configure_paths()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("head")
    sub.add_parser("verify")
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--agent", required=True)
    rebuild.add_argument("--run", required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("commit")
    c.add_argument("--file", required=True, help="JSON file: {type, body, ...}")
    args = ap.parse_args(argv)

    if args.cmd == "head":
        seq, h, problems = verify_chain()
        print(json.dumps({"seq": seq, "hash": h, "problems": problems,
                          "protocol": read_protocol()}, indent=2))
        return 0 if not problems else 1
    if args.cmd == "verify":
        seq, h, problems = verify_chain()
        total = len(event_files())
        ok = not problems
        print(json.dumps({"ok": ok, "valid_prefix_seq": seq, "events_on_disk": total,
                          "head_hash": h, "problems": problems}, indent=2))
        return 0 if ok else 1
    if args.cmd == "rebuild":
        try:
            print(json.dumps(guarded_rebuild_projections(
                agent_id=args.agent, run_id=args.run,
                run_token=os.environ.get("AVCOORD_RUN_TOKEN", ""),
                lease_proofs=json.loads(os.environ.get("AVCOORD_LEASE_PROOFS_JSON", "[]")),
            ), indent=2))
            return 0
        except (ValueError, RuntimeError, PermissionError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
    if args.cmd == "replay":
        print(json.dumps(replay(dry_run=args.dry_run), indent=2))
        return 0
    if args.cmd == "commit":
        spec = json.loads(Path(args.file).read_text(encoding="utf-8"))
        etype = spec.pop("type")
        body = spec.pop("body", {})
        print(json.dumps(commit(etype, body, **spec), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
