#!/usr/bin/env python3
"""Cursor PreToolUse: deny contested writes without a live avcoord lease.

Ambient Kernel (AK-SPWS / ADR-009): this hook is the out-of-band coordination
plane. Agents should `avcoord hydrate` at boot instead of parsing PROTOCOL.
Lease enforcement stays fail-closed here; optional reservation heartbeats live
under MemoryBank/coord/lock/reservations/.

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

    # target_file is what Cursor's edit tool actually sends; without it every
    # Cursor edit denied as "no verifiable target path" regardless of lease.
    # "command" is deliberately absent — see the module docstring.
    for key in ("path", "file_path", "target_file", "target_notebook",
                "filePath", "notebook_path", "relative_workspace_path"):
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


def allow(msg: str | None = None, code: str = "") -> int:
    body: dict = {"permission": "allow"}
    if code:
        body["reason_code"] = code
    if msg:
        body["agent_message"] = msg
    print(json.dumps(body))
    return 0


def deny(agent_msg: str, user_msg: str | None = None, code: str = "") -> int:
    # reason_code exists so tests assert WHY a call was denied. Asserting only
    # the verdict let a test pass for the wrong reason: the Shell case below
    # "passed" for months because extract_paths returned nothing, not because
    # any policy rejected it.
    body = {
        "permission": "deny",
        "agent_message": agent_msg,
        "user_message": user_msg or agent_msg,
    }
    if code:
        body["reason_code"] = code
    print(json.dumps(body))
    return 0


READ_ONLY_TOOLS = frozenset({
    "Read", "read_file", "Grep", "grep", "Glob", "glob", "List", "list_dir",
    "codebase_search", "file_search", "web_search", "fetch_rules",
})

SHELL_TOOLS = frozenset({"Shell", "shell", "run_terminal_cmd", "terminal"})

MUTATING_TOOLS = frozenset({
    "Write", "StrReplace", "EditNotebook", "Delete", "ApplyPatch", "Rename",
    "write", "search_replace", "apply_patch", "rename", "edit_file",
    "create_file", "delete_file",
})

# Shell has no target path by construction, so it cannot be lease-checked per
# path. A write-target DETECTOR was rejected: it must parse arbitrary shell and
# fails OPEN on anything it cannot parse (bash -c "$(...)", python -c, heredocs,
# make). An allowlist inverts the burden — unrecognised input is simply not on
# the list, so it fails CLOSED. Same doctrine the release scanner already uses.
SHELL_ALLOWLIST = {
    "git": frozenset({"status", "log", "diff", "show", "rev-parse", "branch",
                      "ls-files", "worktree", "remote", "describe", "blame"}),
    "bin/avcoord": frozenset({"status", "doctor", "query", "hydrate", "check-lease"}),
    "python3": frozenset({"-m"}),        # narrowed below to pytest --collect-only
}
SHELL_BARE_VERBS = frozenset({
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "file", "stat",
    "du", "df", "realpath", "basename", "dirname", "echo", "pwd", "date",
    "which", "uname", "true", "sort", "uniq", "diff",
})
# Interpreters and trampolines: allowing these would allow everything.
SHELL_FORBIDDEN_HEADS = frozenset({
    "env", "sh", "bash", "zsh", "python", "python3", "perl", "ruby", "node",
    "xargs", "eval", "exec", "sudo", "nohup", "time", "watch",
})
SHELL_METACHARS = (">", ">>", "|", ";", "&", "`", "$(", "${", "\n")


def shell_decision(payload: dict) -> int:
    """Deny-by-default allowlist for shell invocations."""
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        ti = {}
    command = ti.get("command") or ti.get("cmd") or payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return deny("Shell call carried no inspectable command; denied",
                    code="AV_DENY_NO_TARGET")

    # Scan the RAW string BEFORE shlex.split. shlex strips quoting, after which
    # `echo "a > b"` and `echo a > b` are indistinguishable.
    for meta in SHELL_METACHARS:
        if meta in command:
            return deny(
                f"Shell command contains {meta!r}; redirection, pipes and "
                f"substitution are not allowlisted. Claim a lease to run it: "
                f"bin/avcoord claim --agent <you> --resource <path>",
                code="AV_DENY_SHELL_METACHAR")

    import shlex
    try:
        argv = shlex.split(command)
    except ValueError:
        return deny("Shell command could not be parsed; denied",
                    code="AV_DENY_SHELL_METACHAR")
    if not argv:
        return deny("Shell command was empty; denied", code="AV_DENY_NO_TARGET")

    head = argv[0]
    base = head.rsplit("/", 1)[-1]
    if base in SHELL_FORBIDDEN_HEADS and head != "bin/avcoord":
        return deny(f"{base!r} can execute arbitrary code; not allowlisted",
                    code="AV_DENY_SHELL_NOT_ALLOWLISTED")

    ok = False
    if base in SHELL_BARE_VERBS:
        ok = True
    elif head in SHELL_ALLOWLIST or base in SHELL_ALLOWLIST:
        key = head if head in SHELL_ALLOWLIST else base
        sub = argv[1] if len(argv) > 1 else ""
        if key == "python3":
            ok = argv[1:4] == ["-m", "pytest", "--collect-only"]
        else:
            ok = sub in SHELL_ALLOWLIST[key]

    if ok:
        return allow(f"read-only shell verb: {base}", code="AV_ALLOW_READONLY_VERB")

    # Escape hatch, deliberately REQUIRED rather than optional: without it an
    # agent cannot run builds or installs at all, which loses capability. It is
    # not a bypass — claiming is itself lease-checked and audited, so the action
    # becomes attributable and time-bounded instead of silently permitted.
    if agent_holds_a_live_lease():
        return allow(f"non-allowlisted shell permitted under a live lease: {base}",
                     code="AV_ALLOW_LEASED_SHELL")

    return deny(
        f"{base!r} is not an allowlisted read-only verb and you hold no lease. "
        f"Claim one first: bin/avcoord claim --agent "
        f"{os.environ.get('AVCOORD_AGENT', 'orchestrator')} --resource <path>",
        code="AV_DENY_SHELL_NOT_ALLOWLISTED")


def agent_holds_a_live_lease() -> bool:
    root = find_root()
    cli = root / "scripts" / "coord" / "avcoord.py"
    if cli.is_symlink() or not cli.is_file():
        return False
    agent = os.environ.get("AVCOORD_AGENT", "orchestrator")
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(root)
    try:
        proc = subprocess.run(
            [sys.executable, str(cli), "status", "--json"],
            capture_output=True, text=True, env=env, cwd=str(root), timeout=10)
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    for lease in data.get("leases") or []:
        if isinstance(lease, dict) and lease.get("agent_id") == agent:
            return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        return deny("Lease hook received malformed JSON; write denied",
                    code="AV_DENY_MALFORMED")
    if not isinstance(payload, dict):
        return deny("Lease hook received no valid tool payload; write denied",
                    code="AV_DENY_MALFORMED")

    tool = (
        payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
        or ""
    )
    # Read-only tools are the ONLY allowlist. Everything else is gated.
    # The previous logic allowed any tool name it did not recognise, which is
    # fail-OPEN: edit_file, create_file and MCP writers all sailed through.
    # The hook only runs because hooks.json's matcher already classified this
    # call as a mutation channel, so an unknown name is an unvetted writer.
    if str(tool) in READ_ONLY_TOOLS:
        return allow(code="AV_ALLOW_READ_TOOL")

    tool_l = str(tool).lower()
    if str(tool) in SHELL_TOOLS or "shell" in tool_l or "terminal" in tool_l:
        return shell_decision(payload)

    if str(tool) not in MUTATING_TOOLS and not any(
            m in tool_l for m in ("write", "streplace", "editnotebook", "delete",
                                  "edit", "create", "patch", "rename", "move")):
        return deny("Unrecognised tool reached the mutation gate; write denied",
                    code="AV_DENY_UNKNOWN_TOOL")

    paths = extract_paths(payload)
    if not paths:
        return deny("Mutating tool did not provide a verifiable target path; write denied",
                    code="AV_DENY_NO_TARGET")

    root = find_root()
    cli = root / "scripts" / "coord" / "avcoord.py"
    if cli.is_symlink() or not cli.is_file():
        return deny("avcoord lease checker is missing or unsafe; write denied",
                    code="AV_DENY_CHECKER_MISSING")

    agent = os.environ.get("AVCOORD_AGENT", "orchestrator")
    run_id = os.environ.get("AVCOORD_RUN_ID", "")
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(root)

    for path in paths:
        # Pass the path through verbatim: avcoord's canonical kernel does the relativizing
        # and fails closed. The old try/except here silently left an unrelativizable path
        # absolute (a case-variant or symlinked root raises ValueError), and an absolute
        # path is never matched as contested — an exit-0 bypass.
        try:
            command = [sys.executable, str(cli), "check-lease", "--agent", agent, "--path", path]
            if run_id:
                command.extend(["--run", run_id])
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(root),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return deny("avcoord lease checker could not complete; write denied",
                        code="AV_DENY_CHECKER_MISSING")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return deny(
                f"Contested path requires live lease: {path}. "
                f"Run: bin/avcoord claim --agent {agent} --resource <path>. {detail}",
                f"Blocked write to contested path without lease: {path}",
                code="AV_DENY_NO_LEASE",
            )
    return allow(code="AV_ALLOW_LEASED_PATH")


if __name__ == "__main__":
    raise SystemExit(main())
