#!/usr/bin/env python3
"""TTL reservation tokens under coord/lock/reservations/ — crash-safe staging aids.

Complements fcntl FileLock: tokens advertise intent to write a slot/resource;
expired tokens are reapable. Does not replace commits.lock.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from . import store


def _dir(av: Any) -> Path:
    p = store.ensure_kernel_dirs(av)
    return p["lock_res"]


def acquire(
    av: Any,
    *,
    agent_id: str,
    resource: str,
    ttl_sec: int | None = None,
) -> dict[str, Any]:
    cfg = store.load_config(av)
    ttl = int(ttl_sec if ttl_sec is not None else cfg["reservation_ttl_sec"])
    reap(av)
    d = _dir(av)
    # Conflict if another live reservation covers the same resource.
    for path in d.glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if doc.get("resource") == resource and doc.get("agent_id") != agent_id:
            return {"status": "conflict", "holder": doc.get("agent_id"), "token": doc.get("token")}
    token = uuid.uuid4().hex
    now = time.time()
    doc = {
        "token": token,
        "agent_id": agent_id,
        "resource": resource,
        "acquired_at": now,
        "expires_at": now + ttl,
    }
    path = d / f"{token}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", "token": token, "expires_at": doc["expires_at"], "path": str(path)}


def heartbeat(av: Any, token: str, ttl_sec: int | None = None) -> dict[str, Any]:
    cfg = store.load_config(av)
    ttl = int(ttl_sec if ttl_sec is not None else cfg["reservation_ttl_sec"])
    path = _dir(av) / f"{token}.json"
    if not path.exists():
        return {"status": "missing", "token": token}
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["expires_at"] = time.time() + ttl
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return {"status": "ok", "token": token, "expires_at": doc["expires_at"]}


def release(av: Any, token: str) -> dict[str, Any]:
    path = _dir(av) / f"{token}.json"
    if path.exists():
        path.unlink()
        return {"status": "released", "token": token}
    return {"status": "missing", "token": token}


def reap(av: Any) -> list[str]:
    d = _dir(av)
    now = time.time()
    removed: list[str] = []
    for path in list(d.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if float(doc.get("expires_at", 0)) < now:
                path.unlink()
                removed.append(path.name)
        except Exception:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed
