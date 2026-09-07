#!/usr/bin/env python3
"""AgentVault control journal — the single protected writer.

RFC-WORKSPACE-AEAP-20260906 P03. Staged and INACTIVE: this module writes only under
`MemoryBank/coord/{commits,projections}/`. It never touches `CURRENT.md`, `board.md` or
`threads.json`, so nothing here is authoritative until the P07 cutover flips
`protocol.json`'s `authority` field from "legacy" to "journal".

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
import sys
import uuid
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location("_avcoord_lib", Path(__file__).resolve().parent / "avcoord.py")
av = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(av)

SCHEMA_VERSION = "1.0"
ZERO_HASH = "sha256:" + "0" * 64

EVENT_TYPES = {
    "task.transition",
    "artifact.registered",
    "lease.acquired",
    "lease.released",
    "mail.receipt",
    "journal.import",
    "library.admit",
}

REQUIRED_FIELDS = ("schema_version", "seq", "prev_hash", "epoch", "ts", "type", "body")


# ------------------------------------------------------------------------------- paths


def commits_dir() -> Path:
    return av.COORD / "commits"


def payloads_dir() -> Path:
    return commits_dir() / "payloads"


def staging_dir() -> Path:
    return commits_dir() / ".staging"


def quarantine_dir() -> Path:
    return commits_dir() / "_quarantine"


def projections_dir() -> Path:
    return av.COORD / "projections"


def protocol_path() -> Path:
    return av.COORD / "protocol.json"


def ensure_dirs() -> None:
    for d in (commits_dir(), payloads_dir(), staging_dir(), quarantine_dir(), projections_dir(),
              projections_dir() / "tasks", projections_dir() / "artifacts"):
        d.mkdir(parents=True, exist_ok=True)


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


# ------------------------------------------------------------------------------ reading


def event_files() -> list[tuple[int, Path]]:
    """(seq, path) sorted by seq, parsed from filenames — no JSON parse needed."""
    out = []
    if not commits_dir().exists():
        return out
    for p in commits_dir().glob("*.json"):
        stem = p.stem.split("-", 1)[0]
        if stem.isdigit():
            out.append((int(stem), p))
    return sorted(out)


def validate_event(event: dict, expected_seq: int, expected_prev: str) -> str | None:
    """Return a failure reason, or None when the event is valid."""
    for f in REQUIRED_FIELDS:
        if f not in event:
            return f"missing field '{f}'"
    if event["schema_version"] != SCHEMA_VERSION:
        return f"unsupported schema_version {event['schema_version']!r}"
    if event["type"] not in EVENT_TYPES:
        return f"unknown event type {event['type']!r}"
    if event["seq"] != expected_seq:
        return f"seq {event['seq']} != expected {expected_seq}"
    if event["prev_hash"] != expected_prev:
        return f"prev_hash {event['prev_hash']!r} != expected {expected_prev!r}"
    if event.get("hash") != event_hash(event):
        return "hash mismatch (event body was altered)"
    for ref in event.get("payload_refs") or []:
        p = av.COORD / ref["path"] if not str(ref["path"]).startswith("/") else Path(ref["path"])
        if not p.exists():
            return f"missing payload {ref['path']}"
        if hashlib.sha256(p.read_bytes()).hexdigest() != ref["sha256"]:
            return f"payload hash mismatch {ref['path']}"
    return None


def verify_chain() -> tuple[int, str, list[str]]:
    """Return (valid_prefix_seq, head_hash, problems)."""
    problems: list[str] = []
    prev = ZERO_HASH
    last_seq = 0
    for seq, path in event_files():
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(f"{path.name}: unreadable ({e})")
            break
        reason = validate_event(event, last_seq + 1, prev)
        if reason:
            problems.append(f"{path.name}: {reason}")
            break
        prev = event["hash"]
        last_seq = seq
    return last_seq, prev, problems


def read_prefix() -> list[dict]:
    """Every event in the complete valid prefix, in order."""
    out: list[dict] = []
    prev = ZERO_HASH
    for seq, path in event_files():
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            break
        if validate_event(event, len(out) + 1, prev):
            break
        out.append(event)
        prev = event["hash"]
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
) -> dict:
    """Append one event. Returns a receipt.

    Ordering (RFC §7): validate -> write payloads and fsync -> stage event and fsync ->
    os.link to publish -> fsync commits dir (THE COMMIT POINT) -> update projections.
    A crash before the link leaves orphan payloads with no authority; a crash after it
    leaves stale projections that `replay` rebuilds.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    ensure_dirs()

    canon_resources, canon_keys = [], []
    for r in resources or []:
        c = av.canon_resource(r)  # raises CanonError on escape / bad glob — fail closed
        canon_resources.append(c.display)
        canon_keys.append(c.key)

    with av.FileLock(av.COORD / "commits.lock"):
        head_seq, head_hash, problems = verify_chain()
        if problems:
            raise RuntimeError(f"journal is not clean, refusing to append: {problems[0]}")

        if idempotency_key:
            for e in read_prefix():
                if e.get("idempotency_key") == idempotency_key:
                    return {"status": "duplicate", "seq": e["seq"], "hash": e["hash"],
                            "idempotency_key": idempotency_key}

        payload_refs = []
        staged_payloads: list[Path] = []
        for data in payloads or []:
            h = payload_hash(data)
            pp = payloads_dir() / f"{h}.json"
            if not dry_run and not pp.exists():
                av.atomic_write_text(pp, payload_bytes(data).decode("utf-8"))
                staged_payloads.append(pp)
            payload_refs.append({"path": f"commits/payloads/{h}.json", "sha256": h})

        event = {
            "schema_version": SCHEMA_VERSION,
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
        final = commits_dir() / f"{event['seq']:08d}-{event['hash'][7:19]}.json"

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

        rebuild_projections()
        av.audit("journal_commit", seq=event["seq"], type=event_type, agent=agent_id, hash=event["hash"])
        return {"status": "committed", "seq": event["seq"], "hash": event["hash"], "path": str(final)}


# -------------------------------------------------------------------------- projections


def fold(events: list[dict]) -> dict:
    """Fold the valid prefix into projection state. Pure — no I/O."""
    tasks: dict[str, dict] = {}
    artifacts: dict[str, dict] = {}
    threads: dict[str, dict] = {}
    library: dict[str, dict] = {}

    for e in events:
        b = e.get("body") or {}
        if e["type"] == "task.transition":
            tid = b.get("task_id") or e.get("task_id")
            if tid:
                t = tasks.setdefault(tid, {"task_id": tid, "revision": 0, "evidence_refs": []})
                t.update({k: v for k, v in b.items() if k != "task_id"})
                t["revision"] += 1
                t["journal_seq"] = e["seq"]
                t["updated_at"] = e["ts"]
        elif e["type"] == "artifact.registered":
            aid = b.get("artifact_id")
            if aid:
                artifacts[aid] = {**b, "journal_seq": e["seq"], "registered_at": e["ts"]}
        elif e["type"] == "library.admit":
            fid = b.get("factor_id")
            if fid:
                library[fid] = {**b, "journal_seq": e["seq"], "admitted_at": e["ts"]}
        elif e["type"] == "journal.import":
            for t in b.get("threads") or []:
                if t.get("id"):
                    threads[t["id"]] = {**t, "journal_seq": e["seq"]}
    return {"tasks": tasks, "artifacts": artifacts, "threads": threads, "library": library}


def rebuild_projections() -> dict:
    """Regenerate every projection from the journal. Idempotent; _meta written last."""
    ensure_dirs()
    events = read_prefix()
    state = fold(events)
    head_seq = events[-1]["seq"] if events else 0
    head_hash = events[-1]["hash"] if events else ZERO_HASH
    stamp = av.iso()

    tasks_dir = projections_dir() / "tasks"
    for tid, t in state["tasks"].items():
        av.atomic_write_json(tasks_dir / f"{tid}.json", t)

    # Staged location. At P07 cutover this becomes MemoryBank/coord/artifacts/index.json.
    av.atomic_write_json(projections_dir() / "artifacts" / "index.json",
                         {"schema_version": "1.0", "journal_seq": head_seq, "journal_hash": head_hash,
                          "generated_at": stamp, "artifacts": list(state["artifacts"].values())})
    av.atomic_write_json(projections_dir() / "library.json",
                         {"schema_version": "1.0", "journal_seq": head_seq, "journal_hash": head_hash,
                          "generated_at": stamp, "factors": list(state["library"].values())})
    av.atomic_write_json(projections_dir() / "threads.json",
                         {"schema_version": "1.0", "journal_seq": head_seq, "journal_hash": head_hash,
                          "generated_at": stamp, "threads": list(state["threads"].values())})
    meta = {"schema_version": "1.0", "journal_seq": head_seq, "journal_hash": head_hash,
            "generated_at": stamp, "event_count": len(events),
            "authority": read_protocol().get("authority", "legacy")}
    av.atomic_write_json(projections_dir() / "_meta.json", meta)  # last: it attests the rest
    av.fsync_dir(projections_dir())
    return meta


def replay(dry_run: bool = False) -> dict:
    """Quarantine everything after the valid prefix, then rebuild projections."""
    ensure_dirs()
    valid_seq, head_hash, problems = verify_chain()
    bad = [(s, p) for s, p in event_files() if s > valid_seq]
    plan = {"valid_prefix_seq": valid_seq, "head_hash": head_hash, "problems": problems,
            "would_quarantine": [p.name for _, p in bad]}
    if dry_run:
        plan["status"] = "dry_run"
        return plan
    for _, p in bad:
        dest = quarantine_dir() / p.name
        os.replace(p, dest)
        (quarantine_dir() / f"{p.stem}.reason.txt").write_text(
            "\n".join(problems) or "after first invalid event", encoding="utf-8"
        )
    av.fsync_dir(quarantine_dir())
    av.fsync_dir(commits_dir())
    plan["quarantined"] = [p.name for _, p in bad]
    plan["meta"] = rebuild_projections()
    plan["status"] = "replayed"
    if bad:
        av.audit("journal_replay", quarantined=len(bad), valid_prefix_seq=valid_seq)
    return plan


# --------------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    av.configure_paths()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("head")
    sub.add_parser("verify")
    sub.add_parser("rebuild")
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
        ok = not problems and total == seq
        print(json.dumps({"ok": ok, "valid_prefix_seq": seq, "events_on_disk": total,
                          "head_hash": h, "problems": problems}, indent=2))
        return 0 if ok else 1
    if args.cmd == "rebuild":
        print(json.dumps(rebuild_projections(), indent=2))
        return 0
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
