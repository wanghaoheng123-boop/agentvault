#!/usr/bin/env python3
"""Journal-authoritative intent and compatibility helpers.

SQLite, snapshots and projections are caches. Preconditions are checked against
accepted events and pinned to the same journal head checked by the guarded writer.
A stale caller precondition is never silently refreshed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import store

AK_EVENT_TYPES = {"fingerprint.promoted", "fingerprint.rejected", "intent.recorded", "snapshot.created"}


def _load_commit_worker(av: Any) -> Any:
    path = Path(av.__file__).resolve().parent / "commit_worker.py"
    spec = importlib.util.spec_from_file_location(f"cw_for_{id(av)}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.av.configure_paths(av.ROOT)
    # Event authorization belongs to the writer; never extend its allowlist here.
    return mod


def read_config(av: Any) -> dict[str, Any]:
    """Read optional configuration without creating directories, SQLite or files."""
    cfg = dict(store.DEFAULT_CONFIG)
    path = store.paths_from_av(av)["config"]
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in cfg:
            cfg[key] = value if key == "schema_version" else int(value)
    for key in cfg:
        if key != "schema_version" and (not isinstance(cfg[key], int) or cfg[key] <= 0):
            raise ValueError(f"invalid positive kernel configuration: {key}")
    return cfg


def journal_events(cw: Any) -> list[dict]:
    """Complete validated archive+live chain; no compact-cursor trust."""
    seq, digest, problems = cw.verify_chain()
    if problems:
        raise RuntimeError(f"journal_unclean:{problems[0]}")
    events = cw.read_prefix()
    actual = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)
    if actual != (seq, digest):
        raise RuntimeError("journal_changed_during_read; retry from a fresh head")
    return events


def journal_slots(cw: Any, events: list[dict] | None = None) -> list[dict[str, Any]]:
    """Reconstruct slots solely from accepted intent events, including legacy receipts."""
    slots: dict[str, dict] = {}
    for event in journal_events(cw) if events is None else events:
        if event["type"] != "intent.recorded":
            continue
        slot = (event.get("body") or {}).get("slot")
        if not slot:
            continue
        sid = slot.get("id")
        version = slot.get("version")
        if not isinstance(sid, str) or ":" not in sid or not isinstance(version, int) or isinstance(version, bool):
            raise RuntimeError("invalid journal slot receipt")
        kind, entity = sid.split(":", 1)
        if not kind or not entity or not isinstance(slot.get("value"), dict):
            raise RuntimeError("invalid journal slot value")
        current = slots.get(sid)
        expected = 1 if current is None else current["version"] + 1
        if version != expected:
            raise RuntimeError(f"noncontiguous journal slot version:{sid}")
        slots[sid] = {"id": sid, "kind": kind, "version": version,
                      "value": slot["value"], "updated_at": event["ts"]}
    return [slots[key] for key in sorted(slots)]


def check_expected(cw: Any, expected: dict[str, Any] | None, conn: Any | None = None) -> str | None:
    """Validate preconditions using journal folds; conn retained only for API compatibility."""
    events = journal_events(cw)
    head_seq, head_hash = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)
    if not expected:
        return None
    if "journal_seq" in expected and expected["journal_seq"] != head_seq:
        return f"journal_seq:{expected['journal_seq']}!={head_seq}"
    if "journal_hash" in expected and expected["journal_hash"] != head_hash:
        return "journal_hash_mismatch"
    slots = {s["id"]: s for s in journal_slots(cw, events)}
    for sid, version in (expected.get("slots") or {}).items():
        actual = slots.get(sid, {}).get("version", 0)
        if version != actual:
            return f"slot:{sid}:expected={version}:actual={actual}"
    tasks = cw.fold(events).get("tasks") or {}
    for tid, revision in (expected.get("task_revision") or {}).items():
        actual = tasks.get(tid, {}).get("revision", 0)
        if revision != actual:
            return f"task:{tid}:expected_rev={revision}:actual={actual}"
    if "workspace_revision" in expected:
        actual = cw.fold(events).get("workspace", {}).get("revision", 0)
        if expected["workspace_revision"] != actual:
            return f"workspace:expected_rev={expected['workspace_revision']}:actual={actual}"
    if "authority_revision" in expected:
        actual = cw.fold(events).get("authority", {}).get("revision", 0)
        if expected["authority_revision"] != actual:
            return f"authority:expected_rev={expected['authority_revision']}:actual={actual}"
    return None


def commit_with_rebase(
    av: Any, event_type: str, body: dict[str, Any], *,
    agent_id: str = "orchestrator", run_id: str = "", task_id: str = "",
    resources: list[str] | None = None, idempotency_key: str = "",
    parent_id: str | None = None, payloads: list[Any] | None = None,
    expected: dict[str, Any] | None = None,
    body_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    max_retries: int | None = None, dry_run: bool = False, pin_journal: bool = True,
    run_token: str = "", lease_proofs: Any = None,
) -> dict[str, Any]:
    """Submit once against an explicit head; caller must recompute after a conflict.

    The legacy name/signature remains, but retries never refresh stale evaluation,
    library, task or slot dependencies. Only absent head fields are pinned initially.
    The guarded writer validates credentials and all preconditions before any write.
    """
    cw = _load_commit_worker(av)
    try:
        events = journal_events(cw)
        head_seq, head_hash = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)
        use_expected = dict(expected or {})
        use_expected.setdefault("journal_seq", head_seq)
        use_expected.setdefault("journal_hash", head_hash)
        state = cw.fold(events)
        if event_type in {"task.created", "task.transition", "review.receipt"}:
            tid = body.get("task_id") or task_id
            revisions = dict(use_expected.get("task_revision") or {})
            revisions.setdefault(tid, (state.get("tasks") or {}).get(tid, {}).get("revision", 0))
            use_expected["task_revision"] = revisions
        if event_type in {"workspace.context", "workspace.verified"}:
            use_expected.setdefault("workspace_revision", (state.get("workspace") or {}).get("revision", 0))
        if event_type == "authority.changed":
            use_expected.setdefault("authority_revision", (state.get("authority") or {}).get("revision", 0))
        conflict = check_expected(cw, use_expected)
        if conflict:
            return {"status": "conflict", "reason": conflict, "attempts": 1}
        use_body = body_fn({"head_seq": head_seq, "head_hash": head_hash, "attempt": 0}) if body_fn else body
        run_id = run_id or os.environ.get("AVCOORD_RUN_ID", os.environ.get("AVCOORD_RUN", ""))
        run_token = run_token or os.environ.get("AVCOORD_RUN_TOKEN", "")
        if lease_proofs is None and os.environ.get("AVCOORD_LEASE_PROOFS_JSON"):
            lease_proofs = json.loads(os.environ["AVCOORD_LEASE_PROOFS_JSON"])
        receipt = cw.commit(event_type, use_body, agent_id=agent_id, run_id=run_id,
                            task_id=task_id, resources=resources, idempotency_key=idempotency_key,
                            parent_id=parent_id, payloads=payloads, expected=use_expected,
                            dry_run=dry_run, run_token=run_token, lease_proofs=lease_proofs)
        return {**receipt, "attempts": 1}
    except (ValueError, RuntimeError, PermissionError) as error:
        message = str(error)
        conflict = "CAS" in message or "concurrent" in message or "journal_changed" in message
        return {"status": "conflict" if conflict else "error", "reason": message, "attempts": 1}


def intent_commit(av: Any, *, agent_id: str, note: str, task_id: str = "",
                  expect_slot: str | None = None, slot_value: dict[str, Any] | None = None,
                  idempotency_key: str = "", run_id: str = "", run_token: str = "",
                  lease_proofs: Any = None) -> dict[str, Any]:
    """Commit a slot transition in the journal; SQLite is never the write authority."""
    cw = _load_commit_worker(av)
    try:
        events = journal_events(cw)
        head_seq, head_hash = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)
        expected: dict[str, Any] = {"journal_seq": head_seq, "journal_hash": head_hash}
        slot_receipt = None
        if expect_slot:
            kind, entity, version = store.parse_expect_slot(expect_slot)
            if not kind or not entity or version < 0:
                raise ValueError("slot identity and nonnegative version required")
            sid = store.slot_key(kind, entity)
            slots = {s["id"]: s for s in journal_slots(cw, events)}
            actual = slots.get(sid, {}).get("version", 0)
            if actual != version:
                return {"status": "conflict", "reason": f"slot:{sid}:expected={version}:actual={actual}"}
            value = slot_value if slot_value is not None else {"note": note, "task_id": task_id or entity,
                                                               "agent_id": agent_id}
            if not isinstance(value, dict):
                raise ValueError("slot value must be an object")
            slot_receipt = {"status": "ok", "id": sid, "kind": kind, "version": version + 1, "value": value}
            expected["slots"] = {sid: version}
        return commit_with_rebase(av, "intent.recorded",
                                  {"note": note, "task_id": task_id, "slot": slot_receipt,
                                   "head_seq_at_apply": head_seq}, agent_id=agent_id,
                                  task_id=task_id, expected=expected, idempotency_key=idempotency_key,
                                  run_id=run_id, run_token=run_token, lease_proofs=lease_proofs)
    except (ValueError, RuntimeError) as error:
        return {"status": "error", "reason": str(error)}
