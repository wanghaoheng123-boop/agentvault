#!/usr/bin/env python3
"""Journal-derived snapshots and archival; caches never seed journal authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import commit_rebased, store


def _cw(av: Any) -> Any:
    return commit_rebased._load_commit_worker(av)


def merkle_root(event_hashes: list[str]) -> str:
    """Pinned historical name: ordered SHA-256 commitment, not a branching Merkle tree."""
    if not event_hashes:
        return "sha256:" + "0" * 64
    return "sha256:" + hashlib.sha256("\n".join(event_hashes).encode("utf-8")).hexdigest()


def live_commit_bytes(cw: Any) -> int:
    return sum(path.stat().st_size for _, path in cw.event_files()
               if path.parent == cw.commits_dir())


def _snapshots(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "snapshot.created"]


def should_compact(av: Any, *, force: bool = False) -> dict[str, Any]:
    cfg, cw = commit_rebased.read_config(av), _cw(av)
    try:
        events = commit_rebased.journal_events(cw)
    except RuntimeError as error:
        return {"due": False, "problems": [str(error)]}
    head_seq, head_hash = (events[-1]["seq"], events[-1]["hash"]) if events else (0, cw.ZERO_HASH)
    snapshots = _snapshots(events)
    last_snapshot = snapshots[-1] if snapshots else None
    last_seq = int(last_snapshot["body"]["last_seq"]) if last_snapshot else 0
    since, nbytes = head_seq - last_seq, live_commit_bytes(cw)
    return {"due": force or since >= cfg["compact_every_n"] or nbytes >= cfg["compact_max_bytes"],
            "force": force, "head_seq": head_seq, "head_hash": head_hash,
            "last_snap_seq": last_seq, "commits_since_snapshot": since, "live_bytes": nbytes,
            "threshold_n": cfg["compact_every_n"], "threshold_bytes": cfg["compact_max_bytes"],
            "problems": []}


def events_after_snapshot(cw: Any, conn: Any = None) -> list[dict]:
    events = commit_rebased.journal_events(cw)
    snapshots = _snapshots(events)
    last_seq = int(snapshots[-1]["body"]["last_seq"]) if snapshots else 0
    return [e for e in events if e["seq"] > last_seq]


def _snapshot_doc(cw: Any, events: list[dict], epoch: int) -> dict[str, Any]:
    return {"schema_version": "1.0", "epoch": epoch, "first_seq": 1,
            "last_seq": events[-1]["seq"], "head_hash": events[-1]["hash"],
            "merkle_root": merkle_root([e["hash"] for e in events]),
            "folded": cw.fold(events), "created_at": events[-1]["ts"], "event_count": len(events)}


def load_latest_snapshot_doc(av: Any) -> dict[str, Any] | None:
    """Reconstruct an accepted snapshot from original events, ignoring DB/disk caches."""
    cw = _cw(av)
    events = commit_rebased.journal_events(cw)
    for event in reversed(_snapshots(events)):
        body = event.get("body") or {}
        last_seq, epoch = body.get("last_seq"), body.get("epoch")
        if not isinstance(last_seq, int) or not isinstance(epoch, int) or not 0 < last_seq < event["seq"]:
            continue
        prefix = [e for e in events if e["seq"] <= last_seq]
        if not prefix or prefix[-1]["seq"] != last_seq:
            continue
        doc = _snapshot_doc(cw, prefix, epoch)
        if body.get("merkle_root") != doc["merkle_root"]:
            continue
        if body.get("head_hash", doc["head_hash"]) != doc["head_hash"]:
            continue
        return doc
    return None


def compact(av: Any, *, force: bool = False, agent_id: str = "orchestrator",
            run_id: str = "", run_token: str = "", lease_proofs: Any = None) -> dict[str, Any]:
    """Accept snapshot journal event first, then materialize nonauthoritative caches.

    Missing credentials or stale dependencies cause no kernel/config/database writes.
    The archive remains part of every full-chain verification and replay.
    """
    check = should_compact(av, force=force)
    if check["problems"]:
        return {"status": "error", "reason": "journal_unclean", **check}
    if not check["due"] or check["head_seq"] == 0:
        return {"status": "skipped", **check}
    cw = _cw(av)
    try:
        events = commit_rebased.journal_events(cw)
        expected = {"journal_seq": check["head_seq"], "journal_hash": check["head_hash"]}
        if not events or events[-1]["seq"] != check["head_seq"] or events[-1]["hash"] != check["head_hash"]:
            return {"status": "conflict", "reason": "journal changed before snapshot"}
        epochs = [e.get("body", {}).get("epoch", 0) for e in _snapshots(events)]
        epoch = max([n for n in epochs if isinstance(n, int) and not isinstance(n, bool)] + [0]) + 1
        doc = _snapshot_doc(cw, events, epoch)
        out_path = store.paths_from_av(av)["snapshots"] / f"state_v{epoch}.json"
        rel = str(out_path.relative_to(av.ROOT))
        body = {key: doc[key] for key in ("epoch", "first_seq", "last_seq", "head_hash", "merkle_root")}
        body["path"] = rel
        journal = commit_rebased.commit_with_rebase(
            av, "snapshot.created", body, agent_id=agent_id, expected=expected,
            payloads=[doc], idempotency_key=f"snapshot-{doc['last_seq']}-{doc['head_hash'][7:]}",
            resources=["MemoryBank/coord/snapshots/**"], run_id=run_id,
            run_token=run_token, lease_proofs=lease_proofs)
        if journal.get("status") not in ("committed", "duplicate"):
            return {"status": journal.get("status", "error"), "journal": journal,
                    "reason": journal.get("reason", "snapshot was not accepted")}
        # Serialize with lease changes and all journal publishers, then revalidate the
        # exact fence/token generation before snapshot, archive, or cursor writes.
        with cw.av.coord_lock():
            cw.authorize(["MemoryBank/coord/snapshots/**", "MemoryBank/coord/commits/**"],
                         agent_id, run_id, run_token, lease_proofs)
            accepted = commit_rebased.journal_events(cw)
            match = next((e for e in accepted if e["seq"] == journal["seq"]), None)
            if (match is None or match["hash"] != journal["hash"]
                    or match.get("body") != body):
                raise RuntimeError("accepted snapshot receipt no longer matches journal")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.exists() and json.loads(out_path.read_text(encoding="utf-8")) != doc:
                raise RuntimeError("snapshot cache collision; preserving existing file")
            if not out_path.exists():
                av.atomic_write_json(out_path, doc)
            archived = _archive_commits_through(cw, doc["last_seq"], keep_after=True)
            cw.write_compact_cursor({"schema_version": "1.0", "last_seq": doc["last_seq"],
                                    "head_hash": doc["head_hash"], "merkle_root": doc["merkle_root"],
                                    "snapshot_epoch": epoch, "snapshot_path": rel,
                                    "archived_at": match["ts"], "authority": "cache_only"})
        return {"status": "compacted", "epoch": epoch, "path": rel,
                "last_seq": doc["last_seq"], "merkle_root": doc["merkle_root"],
                "archived": archived, "journal": journal}
    except (ValueError, OSError, RuntimeError) as error:
        return {"status": "error", "reason": str(error)}


def _archive_commits_through(cw: Any, last_seq: int, *, keep_after: bool) -> list[str]:
    """Move complete events under the caller's coord.lock; never overwrite history."""
    archive = cw.commits_dir() / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    moved = []
    for seq, path in cw.event_files():
        if seq > last_seq or path.parent != cw.commits_dir():
            continue
        dest = archive / path.name
        if dest.exists():
            if dest.read_bytes() != path.read_bytes():
                raise RuntimeError(f"archive collision: {path.name}")
            continue
        os.rename(path, dest)
        moved.append(path.name)
    if moved:
        cw.av.fsync_dir(archive)
        cw.av.fsync_dir(cw.commits_dir())
    return moved


def read_prefix_with_snapshot(av: Any) -> tuple[dict[str, Any] | None, list[dict]]:
    """Return only a journal-reconstructed snapshot and validated contiguous delta."""
    cw = _cw(av)
    events = commit_rebased.journal_events(cw)
    doc = load_latest_snapshot_doc(av)
    if doc is None:
        return None, events
    return doc, [e for e in events if e["seq"] > doc["last_seq"]]
