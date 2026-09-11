#!/usr/bin/env python3
"""Atomic reader/writer for .agentvault/tasks/board.json.

Exists because board.json is shared live across every worktree (it is reached
through a symlink), which makes it a contested file. Hand-editing it from a
shell hook would violate the invariants in
.agentvault/invariants/concurrency-and-data.md:

  C3  all writes are temp-file + os.replace, never open(path, "w")
  C7  re-read before write; another worktree may have changed it
  C9  operations are idempotent ("set to state X", never "increment")

Usage:
  av-board.py get   [--id ID]
  av-board.py set   --id ID [--title T] [--branch B] [--status S]
                    [--agent A] [--handoff F] [--notes N]
  av-board.py claim --id ID --agent A [--branch B]      # -> IN_PROGRESS
  av-board.py pause --id ID [--handoff F]               # -> NEEDS_CONTINUATION
  av-board.py merged --id ID                            # -> MERGED

`set` and `claim` create the task if it does not exist, so hooks can call them
unconditionally. Exit codes: 0 ok, 2 usage/not-found, 3 invalid status.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BOARD = Path(__file__).resolve().parent.parent / "tasks" / "board.json"
LOCK = BOARD.with_suffix(".json.lock")

STATUSES = [
    "UNASSIGNED",
    "IN_PROGRESS",
    "BLOCKED",
    "NEEDS_CONTINUATION",
    "READY_FOR_REVIEW",
    "MERGED",
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    if not BOARD.exists():
        return {"schema": "agentvault-board-v1", "status_values": STATUSES, "tasks": []}
    with BOARD.open() as fh:
        data = json.load(fh)
    data.setdefault("tasks", [])
    return data


def save(data: dict) -> None:
    """Temp + os.replace on the same filesystem (invariant C3)."""
    data["updated_at"] = now()
    BOARD.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(BOARD.parent), prefix=".board-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, BOARD)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def mutate(fn):
    """Run fn(data) under an exclusive lock, re-reading inside the lock (C7)."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = load()
            rc = fn(data)
            save(data)
            return rc
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def slug(branch: str) -> str:
    """Flatten slashes so `feat/payment` -> `feat-payment`.

    Handoff files are flat (`handoffs/<slug>.md`); a raw branch name with a
    slash would name a nested directory that does not exist and the write
    would fail. Matches templates.worktree_path's `replace('/', '-')`.
    """
    return branch.replace("/", "-")


def find(data: dict, task_id: str):
    for t in data["tasks"]:
        if t.get("id") == task_id:
            return t
    return None


def upsert(data: dict, task_id: str) -> dict:
    t = find(data, task_id)
    if t is None:
        t = {
            "id": task_id,
            "title": task_id,
            "branch": None,
            "status": "UNASSIGNED",
            "assigned_agent": None,
            "handoff_file": None,
            "updated_at": now(),
        }
        data["tasks"].append(t)
    return t


def apply_fields(t: dict, a: argparse.Namespace) -> None:
    if getattr(a, "title", None):
        t["title"] = a.title
    if getattr(a, "branch", None):
        t["branch"] = a.branch
    if getattr(a, "agent", None):
        t["assigned_agent"] = a.agent
    if getattr(a, "handoff", None):
        t["handoff_file"] = a.handoff
    if getattr(a, "notes", None):
        t["notes"] = a.notes
    t["updated_at"] = now()


def main() -> int:
    p = argparse.ArgumentParser(prog="av-board.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="print the board, or one task")
    g.add_argument("--id")

    for name in ("set", "claim", "pause", "merged"):
        s = sub.add_parser(name)
        s.add_argument("--id", required=True)
        s.add_argument("--title")
        s.add_argument("--branch")
        s.add_argument("--agent")
        s.add_argument("--handoff")
        s.add_argument("--notes")
        if name == "set":
            s.add_argument("--status")

    a = p.parse_args()

    if a.cmd == "get":
        data = load()
        if a.id:
            t = find(data, a.id)
            if t is None:
                print(f"no such task: {a.id}", file=sys.stderr)
                return 2
            print(json.dumps(t, indent=2))
        else:
            print(json.dumps(data, indent=2))
        return 0

    if a.cmd == "set" and a.status and a.status not in STATUSES:
        print(f"invalid status {a.status!r}; expected one of {STATUSES}", file=sys.stderr)
        return 3

    def op(data: dict) -> int:
        t = upsert(data, a.id)
        apply_fields(t, a)
        if a.cmd == "set":
            if a.status:
                t["status"] = a.status
        elif a.cmd == "claim":
            t["status"] = "IN_PROGRESS"
        elif a.cmd == "pause":
            t["status"] = "NEEDS_CONTINUATION"
            if not t.get("handoff_file") and t.get("branch"):
                t["handoff_file"] = f".agentvault/handoffs/{slug(t['branch'])}.md"
        elif a.cmd == "merged":
            t["status"] = "MERGED"
        return 0

    rc = mutate(op)
    print(json.dumps(find(load(), a.id), indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
