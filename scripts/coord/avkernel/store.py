#!/usr/bin/env python3
"""SQLite slot store + kernel config for AK-SPWS.

Authority for audit remains the hash-chained journal. This DB is the fast
OCC working set: one-active-value-per-slot with integer versions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# Defaults when config.yaml is missing.
DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "1.0",
    "compact_every_n": 50,
    "compact_max_bytes": 2_000_000,
    "hydrate_token_budget": 1200,
    "rebase_max_retries": 8,
    "reservation_ttl_sec": 120,
}


def paths_from_av(av: Any) -> dict[str, Path]:
    coord = av.COORD
    kernel = coord / "kernel"
    return {
        "coord": coord,
        "kernel": kernel,
        "db": kernel / "avkernel.sqlite",
        "config": kernel / "config.yaml",
        "snapshots": coord / "snapshots",
        "lock_res": coord / "lock" / "reservations",
        "fingerprint": av.ROOT / "MemoryBank" / "fingerprint",
    }


def ensure_kernel_dirs(av: Any) -> dict[str, Path]:
    p = paths_from_av(av)
    for key in ("kernel", "snapshots", "lock_res"):
        p[key].mkdir(parents=True, exist_ok=True)
    fp = p["fingerprint"]
    for sub in ("hypothesis", "candidate", "approved", "rejected", "proofs",
                "eval/schemas", "eval/fixtures", "eval/counterexamples"):
        (fp / sub).mkdir(parents=True, exist_ok=True)
    if not p["config"].exists():
        write_config(p["config"], DEFAULT_CONFIG)
    return p


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal YAML subset — avoid PyYAML dependency.
    lines = [
        f'schema_version: "{cfg.get("schema_version", "1.0")}"',
        f"compact_every_n: {int(cfg.get('compact_every_n', 50))}",
        f"compact_max_bytes: {int(cfg.get('compact_max_bytes', 2_000_000))}",
        f"hydrate_token_budget: {int(cfg.get('hydrate_token_budget', 1200))}",
        f"rebase_max_retries: {int(cfg.get('rebase_max_retries', 8))}",
        f"reservation_ttl_sec: {int(cfg.get('reservation_ttl_sec', 120))}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def load_config(av: Any) -> dict[str, Any]:
    p = ensure_kernel_dirs(av)
    cfg = dict(DEFAULT_CONFIG)
    text = p["config"].read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in cfg:
            if k == "schema_version":
                cfg[k] = v
            else:
                try:
                    cfg[k] = int(v)
                except ValueError:
                    cfg[k] = v
    return cfg


def connect(av: Any) -> sqlite3.Connection:
    p = ensure_kernel_dirs(av)
    conn = sqlite3.connect(str(p["db"]), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS slots (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            value_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            epoch INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            first_seq INTEGER NOT NULL,
            last_seq INTEGER NOT NULL,
            head_hash TEXT NOT NULL,
            merkle_root TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fingerprint_index (
            id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            path TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def slot_key(kind: str, entity_id: str) -> str:
    return f"{kind}:{entity_id}"


def get_slot(conn: sqlite3.Connection, slot_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "version": int(row["version"]),
        "value": json.loads(row["value_json"]),
        "updated_at": row["updated_at"],
    }


def cas_slot(
    conn: sqlite3.Connection,
    slot_id: str,
    kind: str,
    new_value: dict[str, Any],
    expected_version: int | None,
    ts: str,
) -> dict[str, Any]:
    """Optimistic compare-and-set. expected_version=None means create-if-absent (v0→1)
    or unconditional bump when the slot already exists only if expected is omitted for
    first write — callers should pass 0 for create.
    """
    cur = get_slot(conn, slot_id)
    if cur is None:
        if expected_version not in (None, 0):
            return {
                "status": "conflict",
                "reason": "slot_missing",
                "expected": expected_version,
                "actual": None,
            }
        conn.execute(
            "INSERT INTO slots (id, kind, version, value_json, updated_at) VALUES (?, ?, 1, ?, ?)",
            (slot_id, kind, json.dumps(new_value, sort_keys=True), ts),
        )
        return {"status": "ok", "id": slot_id, "version": 1, "value": new_value}

    if expected_version is not None and cur["version"] != expected_version:
        return {
            "status": "conflict",
            "reason": "version_mismatch",
            "expected": expected_version,
            "actual": cur["version"],
            "value": cur["value"],
        }

    new_ver = cur["version"] + 1
    cursor = conn.execute(
        "UPDATE slots SET version = ?, value_json = ?, updated_at = ?, kind = ? WHERE id = ? AND version = ?",
        (new_ver, json.dumps(new_value, sort_keys=True), ts, kind, slot_id, cur["version"]),
    )
    # The UPDATE itself is the only authority on who won. Re-reading the version cannot
    # decide it: a concurrent winner leaves exactly `new_ver` in the row, so a loser whose
    # UPDATE matched nothing would read back its own intended version and report success for
    # a value that was never stored.
    if cursor.rowcount != 1:
        current = get_slot(conn, slot_id)
        return {
            "status": "conflict",
            "reason": "lost_update",
            "expected": expected_version,
            "actual": None if current is None else current["version"],
            "value": None if current is None else current["value"],
        }
    return {"status": "ok", "id": slot_id, "version": new_ver, "value": new_value}


def list_slots(conn: sqlite3.Connection, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute("SELECT * FROM slots WHERE kind = ? ORDER BY id", (kind,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM slots ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "version": int(r["version"]),
            "value": json.loads(r["value_json"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    epoch: int,
    path: str,
    first_seq: int,
    last_seq: int,
    head_hash: str,
    merkle_root: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO snapshots
        (epoch, path, first_seq, last_seq, head_hash, merkle_root, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (epoch, path, first_seq, last_seq, head_hash, merkle_root, created_at),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('latest_snapshot_epoch', ?)",
        (str(epoch),),
    )


def latest_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM snapshots ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return dict(row)


def index_fingerprint(
    conn: sqlite3.Connection,
    *,
    fid: str,
    stage: str,
    path: str,
    version: int,
    ts: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fingerprint_index (id, stage, path, version, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fid, stage, path, version, ts),
    )


def parse_expect_slot(spec: str) -> tuple[str, str, int]:
    """Parse 'kind:id=vN' or 'kind:id=N' → (kind, id, version)."""
    if "=" not in spec:
        raise ValueError(f"expect-slot must be kind:id=vN, got {spec!r}")
    left, right = spec.rsplit("=", 1)
    if ":" not in left:
        raise ValueError(f"expect-slot must include kind:id, got {spec!r}")
    kind, entity_id = left.split(":", 1)
    ver_s = right[1:] if right.startswith("v") else right
    return kind.strip(), entity_id.strip(), int(ver_s)
