#!/usr/bin/env python3
"""Worktrunk lifecycle writes to the canonical hub under a lease and board lock."""
from __future__ import annotations

import argparse
import fcntl
import json
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location("av_board", Path(__file__).with_name("av-board.py"))
board = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(board)


class LifecycleError(ValueError):
    pass


def git(*args: str) -> str:
    return subprocess.run(["git", *args], text=True, capture_output=True, check=True).stdout.strip()


@contextmanager
def lease(agent: str):
    root = board.HUB.parent
    module_spec = importlib.util.spec_from_file_location("lifecycle_coord", root / "scripts/coord/avcoord.py")
    coord = importlib.util.module_from_spec(module_spec)
    assert module_spec.loader is not None
    module_spec.loader.exec_module(coord)
    coord.configure_paths(root)
    run_id, _, error = coord.resolve_run(argparse.Namespace(agent=agent))
    if error or not coord.validate_agent(agent):
        raise LifecycleError("canonical hub run authorization denied")
    cli = [sys.executable, str(root / "scripts/coord/avcoord.py")]
    binding = ["--run", run_id] if run_id else []
    common = ["--agent", agent, "--resource", ".agentvault", *binding]
    env = dict(os.environ, AVCOORD_ROOT=str(root))
    # Serialize lifecycle calls even when they share a legacy role/run identity.
    with (board.HUB / ".lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with coord.coord_lock():
            needed = coord.canon_resource(".agentvault")
            borrowed = any(
                coord._same_principal(item, agent, run_id)
                and any(coord.res_covers(resource, needed) for resource in coord.lease_resources(item))
                for item in coord.read_leases()
            )
        token = None
        if not borrowed:
            # Capture coordinator output: claim receipts contain lease secrets.
            result = subprocess.run([*cli, "claim", *common, "--ttl", "5", "--reason", "Worktree lifecycle"], cwd=root, env=env, capture_output=True, text=True)
            if result.returncode:
                raise LifecycleError("canonical hub lease denied; inspect avcoord status")
            token = json.loads(result.stdout)["lease_token"]
        try:
            yield
        finally:
            if token is not None:
                result = subprocess.run([*cli, "release", *common, "--token", token], cwd=root, env=env, capture_output=True, text=True)
                if result.returncode:
                    raise LifecycleError("canonical hub lease release failed; inspect avcoord status")



def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def start(agent: str, branch: str, task_id: str):
    def op(data):
        task = board.find(data, task_id)
        if task and task.get("status") == "IN_PROGRESS" and task.get("assigned_agent") not in {None, agent}:
            raise LifecycleError("task is active under another agent")
        if task and task.get("branch") not in {None, branch}:
            raise LifecycleError("task belongs to another branch")
        task = board.upsert(data, task_id)
        task.update(branch=branch, assigned_agent=agent, status="IN_PROGRESS", updated_at=board.now())
        return 0
    board.mutate(op, guard=lease(agent))
    print(f"Task {task_id}: IN_PROGRESS; shared hub: {board.HUB}")


def finish(agent: str, branch: str, task_id: str, commit: str):
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise LifecycleError("source commit must be a full object id")
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], capture_output=True)
    if result.returncode:
        raise LifecycleError("source commit is not merged into target HEAD")
    def op(data):
        task = board.find(data, task_id)
        if task is None or task.get("branch") != branch:
            raise LifecycleError("merged task is missing or belongs to another branch")
        handoff = board.HUB / "handoffs" / f"{board.slug(branch)}.md"
        archive = handoff.parent / "archive" / handoff.name
        if handoff.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            if archive.exists():
                raise LifecycleError("handoff archive already exists; preserve both versions for review")
            os.replace(handoff, archive)
        lessons = board.HUB / "memory/lessons-learned.md"
        text = lessons.read_text() if lessons.exists() else ""
        marker = f"<!-- merge:{commit}:{branch} -->"
        if marker not in text:
            atomic_text(lessons, text + f"\n{marker}\nMerged `{branch}` at `{commit}`; the source commit is an ancestor of target HEAD.\n")
        task.update(status="MERGED", updated_at=board.now())
        if archive.exists():
            task["handoff_file"] = archive.relative_to(board.HUB.parent).as_posix()
        return 0
    board.mutate(op, guard=lease(agent))
    print(f"Task {task_id}: MERGED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "finish"])
    parser.add_argument("--branch")
    parser.add_argument("--commit")
    args = parser.parse_args()
    try:
        branch = args.branch or git("branch", "--show-current")
        if not branch or subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True).returncode:
            raise LifecycleError("a valid branch is required")
        agent = os.environ.get("AVCOORD_AGENT", "orchestrator")
        task_id = os.environ.get("WT_TASK_ID") or branch
        if args.action == "start":
            start(agent, branch, task_id)
        else:
            finish(agent, branch, task_id, args.commit or "")
        return 0
    except (LifecycleError, OSError, subprocess.CalledProcessError) as error:
        print(f"LIFECYCLE BLOCKED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
