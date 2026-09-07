#!/usr/bin/env python3
"""Cursor PreToolUse: deny contested writes without a live avcoord lease.

Env:
  AVCOORD_AGENT  — agent id (default: orchestrator)
  AVCOORD_ROOT   — optional workspace root override
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def find_root() -> Path:
    env = os.environ.get("AVCOORD_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    # .cursor/hooks/ → repo root
    return here.parents[2]


def extract_paths(payload: dict) -> list[str]:
    paths: list[str] = []
    tool_input = payload.get("tool_input") or payload.get("input") or payload.get("arguments") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError:
            tool_input = {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    for key in ("path", "file_path", "target_notebook", "filePath"):
        v = tool_input.get(key) or payload.get(key)
        if isinstance(v, str) and v.strip():
            paths.append(v.strip())

    for key in ("paths", "files"):
        v = tool_input.get(key)
        if isinstance(v, list):
            paths.extend(str(x) for x in v if x)

    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def allow(msg: str | None = None) -> int:
    body: dict = {"permission": "allow"}
    if msg:
        body["agent_message"] = msg
    print(json.dumps(body))
    return 0


def deny(agent_msg: str, user_msg: str | None = None) -> int:
    body = {
        "permission": "deny",
        "agent_message": agent_msg,
        "user_message": user_msg or agent_msg,
    }
    print(json.dumps(body))
    return 0


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return allow()

    tool = (
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
        or ""
    )
    # Only gate mutating file tools
    mutate = {"Write", "StrReplace", "EditNotebook", "Delete", "write", "search_replace"}
    if tool and tool not in mutate and not any(m.lower() in str(tool).lower() for m in ("write", "streplace", "editnotebook", "delete")):
        return allow()

    paths = extract_paths(payload)
    if not paths:
        return allow()

    root = find_root()
    cli = root / "scripts" / "coord" / "avcoord.py"
    if not cli.exists():
        return allow("avcoord missing; lease check skipped")

    agent = os.environ.get("AVCOORD_AGENT", "orchestrator")
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(root)

    for path in paths:
        # Pass the path through verbatim: avcoord's canonical kernel does the relativizing
        # and fails closed. The old try/except here silently left an unrelativizable path
        # absolute (a case-variant or symlinked root raises ValueError), and an absolute
        # path is never matched as contested — an exit-0 bypass.
        proc = subprocess.run(
            [sys.executable, str(cli), "check-lease", "--agent", agent, "--path", path],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(root),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return deny(
                f"Contested path requires live lease: {path}. "
                f"Run: bin/avcoord claim --agent {agent} --resource <path>. {detail}",
                f"Blocked write to contested path without lease: {path}",
            )
    return allow()


if __name__ == "__main__":
    raise SystemExit(main())
