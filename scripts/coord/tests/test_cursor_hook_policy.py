"""Policy tests for the Cursor PreToolUse hook, asserted on REASON CODES.

Asserting only the verdict is how the previous Shell test passed for months
while proving nothing: it expected "deny" for `cat x > protected`, and got it
because extract_paths returned no paths — not because any policy rejected the
redirect. Every case here pins WHY.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / ".cursor" / "hooks" / "avcoord-lease-check.py"


def run_hook(payload: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("AVCOORD_RUN_ID", "AVCOORD_RUN_TOKEN")}
    env["AVCOORD_ROOT"] = str(ROOT)
    # A registered agent that holds no leases. An UNregistered id makes
    # check-lease fail on agent validation instead of on lease state, which
    # would make these tests pass for the wrong reason.
    env["AVCOORD_AGENT"] = "peer_reviewer"
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)
    return json.loads(proc.stdout)


def shell(cmd: str) -> dict:
    return run_hook({"tool_name": "Shell", "tool_input": {"command": cmd}})


# --- Shell: deny by default, allowlist read-only verbs -------------------
@pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "ls -la",
                                 "cat README.md", "rg pattern", "pwd"])
def test_read_only_verbs_are_allowed(cmd):
    r = shell(cmd)
    assert r["permission"] == "allow", cmd
    assert r["reason_code"] == "AV_ALLOW_READONLY_VERB", cmd


@pytest.mark.parametrize("cmd,code", [
    ("cat x > protected",            "AV_DENY_SHELL_METACHAR"),
    ('echo "a > b"',                 "AV_DENY_SHELL_METACHAR"),
    ("git status; rm -rf x",         "AV_DENY_SHELL_METACHAR"),
    ("git status && rm x",           "AV_DENY_SHELL_METACHAR"),
    ("cat a | tee b",                "AV_DENY_SHELL_METACHAR"),
    ("echo $(whoami)",               "AV_DENY_SHELL_METACHAR"),
    ('python3 -c "import os"',       "AV_DENY_SHELL_NOT_ALLOWLISTED"),
    ("bash -c anything",             "AV_DENY_SHELL_NOT_ALLOWLISTED"),
    ("env AVCOORD_ROOT=/tmp git commit", "AV_DENY_SHELL_NOT_ALLOWLISTED"),
    ("xargs rm",                     "AV_DENY_SHELL_NOT_ALLOWLISTED"),
    ("rm -rf /",                     "AV_DENY_SHELL_NOT_ALLOWLISTED"),
    ("git commit -m x",              "AV_DENY_SHELL_NOT_ALLOWLISTED"),
])
def test_shell_writes_and_trampolines_are_denied(cmd, code):
    r = shell(cmd)
    assert r["permission"] == "deny", cmd
    assert r["reason_code"] == code, f"{cmd}: got {r['reason_code']}"


def test_quoted_metachar_is_caught_before_shlex_strips_it():
    """shlex.split removes quoting, after which `echo "a > b"` looks safe."""
    assert shell('echo "a > b"')["reason_code"] == "AV_DENY_SHELL_METACHAR"


# --- F1: Cursor's real edit payload key ----------------------------------
def test_target_file_is_understood():
    """Cursor's edit tool sends target_file; the hook used to ignore it and
    deny every edit as 'no verifiable target path' regardless of lease."""
    r = run_hook({"tool_name": "Write",
                  "tool_input": {"target_file": "MemoryBank/CURRENT.md"}})
    assert r["reason_code"] == "AV_DENY_NO_LEASE", (
        "a contested edit must be denied for lacking a LEASE, not for lacking "
        "a parseable target")


def test_uncontested_target_file_is_allowed():
    r = run_hook({"tool_name": "Write", "tool_input": {"target_file": "README.md"}})
    assert r["permission"] == "allow"


# --- F4: unknown tools fail closed ---------------------------------------
@pytest.mark.parametrize("tool", ["mcp_fs_put", "some_future_tool", "unknown"])
def test_unknown_tools_are_denied_not_allowed(tool):
    r = run_hook({"tool_name": tool, "tool_input": {}})
    assert r["permission"] == "deny", f"{tool} was allowed — fail-open"
    assert r["reason_code"] == "AV_DENY_UNKNOWN_TOOL"


def test_read_only_tools_still_pass():
    r = run_hook({"tool_name": "Read", "tool_input": {"path": "README.md"}})
    assert r["permission"] == "allow"
    assert r["reason_code"] == "AV_ALLOW_READ_TOOL"


# --- malformed input ------------------------------------------------------
def test_malformed_payload_denies():
    env = {k: v for k, v in os.environ.items()}
    env["AVCOORD_ROOT"] = str(ROOT)
    proc = subprocess.run([sys.executable, str(HOOK)], input="{not json",
                          capture_output=True, text=True, env=env, timeout=30)
    assert json.loads(proc.stdout)["reason_code"] == "AV_DENY_MALFORMED"


# --- D7: Shell must stay in the matcher ----------------------------------
def test_shell_remains_a_gated_channel():
    """RFC finding D7 was 'configured matcher omits shell writes'. Adding Shell
    WAS the fix; removing it makes the hook never fire for shell at all."""
    cfg = json.loads((ROOT / ".cursor/hooks.json").read_text())
    hook = cfg["hooks"]["preToolUse"][0]
    assert hook["failClosed"] is True
    matchers = set(hook["matcher"].split("|"))
    for required in ("Write", "Delete", "ApplyPatch", "Rename", "Shell"):
        assert required in matchers, f"{required} missing from the matcher"


# --- Bootstrap: claim must be reachable with zero leases ----------------
# Regression for a deadlock found in the field: an agent could not start work
# at all. `bin/avcoord claim` was not allowlisted and the escape hatch
# requires a lease already held, so a fresh agent could never obtain its first
# lease. The denial message told the agent to run the very command it denied.
#
# Allowlisting `claim` is safe because claim carries its own authorization:
# an overlapping resource held by another principal is refused
# (`resources_overlap` -> return 1 in cmd_claim), verified live.
def test_claim_is_reachable_with_no_lease():
    r = shell("bin/avcoord claim --agent peer_reviewer --resource reports/")
    assert r["permission"] == "allow", r
    assert r["reason_code"] == "AV_ALLOW_BOOTSTRAP_CLAIM", r


def test_bootstrap_does_not_widen_to_other_subcommands():
    # release can target another principal's leases when unbound by a run
    # token, so it stays denied; the escape hatch covers it once a lease is
    # held. init/gate/cutover mutate the substrate.
    for sub in ("init", "release --agent orchestrator --all", "gate",
                "cutover", "done", "rotate-events", "compact"):
        r = shell(f"bin/avcoord {sub}")
        assert r["permission"] == "deny", sub
        assert r["reason_code"] == "AV_DENY_SHELL_NOT_ALLOWLISTED", sub


def test_denial_message_names_the_one_permitted_bootstrap_verb():
    # The old message said "Claim one first: bin/avcoord claim ..." while
    # denying that exact command. Whatever it says now must be true.
    r = shell("make build")
    assert r["permission"] == "deny", r
    assert "bin/avcoord claim" in r["agent_message"], r
    probe = shell("bin/avcoord claim --agent peer_reviewer --resource reports/")
    assert probe["permission"] == "allow", (
        "denial message advertises a command the hook denies", probe)


# --- Escape hatch: the allow-branch, against a REAL lease ----------------
# Every other test in this file runs with zero leases, so they all exercise the
# DENY path and pass whether or not the hatch works. It did not: the hook read
# `lease["agent_id"]` while `status --json` emits `lease["agent"]`, so
# agent_holds_a_live_lease() returned False unconditionally and correction H
# was dead code from the day it landed. Mocking the lease store would have
# reproduced the same blind spot, so this claims a real one.
CLI = ROOT / "scripts" / "coord" / "avcoord.py"


@pytest.fixture
def live_lease():
    res = ".scratch-hatch-test/"
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(ROOT)
    for k in ("AVCOORD_RUN_ID", "AVCOORD_RUN_TOKEN"):
        env.pop(k, None)
    claim = subprocess.run(
        [sys.executable, str(CLI), "claim", "--agent", "peer_reviewer",
         "--resource", res],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=30)
    assert claim.returncode == 0, f"fixture could not claim: {claim.stderr}"
    try:
        yield res
    finally:
        subprocess.run(
            [sys.executable, str(CLI), "release", "--agent", "peer_reviewer",
             "--resource", res],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=30)


def test_escape_hatch_opens_under_a_real_live_lease(live_lease):
    r = shell("make build")
    assert r["permission"] == "allow", r
    assert r["reason_code"] == "AV_ALLOW_LEASED_SHELL", r


def test_escape_hatch_still_refuses_interpreters_under_a_lease(live_lease):
    # A live lease must not turn the hook into a shell. Interpreters are
    # rejected before the hatch is reached.
    for cmd in ("bash -c 'rm -rf /'", "python -c 'import os'", "sudo id"):
        r = shell(cmd)
        assert r["permission"] == "deny", cmd
        assert r["reason_code"] == "AV_DENY_SHELL_NOT_ALLOWLISTED", cmd


def test_escape_hatch_is_closed_without_a_lease():
    r = shell("make build")
    assert r["permission"] == "deny", r


def test_escape_hatch_survives_a_nonzero_status_exit(live_lease):
    """A stale CURRENT.md must not disable the escape hatch.

    `avcoord status` exits nonzero when CURRENT.md is stale or doctor fails --
    conditions with nothing to do with lease state. The hook used to bail on
    `returncode != 0` and report "no lease", so a freshly installed workspace
    (whose CURRENT.md ships with a fixed date and is stale on day one) had a
    dead escape hatch for every adopter, no matter how many leases were held.
    """
    env = os.environ.copy()
    env["AVCOORD_ROOT"] = str(ROOT)
    proc = subprocess.run([sys.executable, str(CLI), "status", "--json"],
                          capture_output=True, text=True, env=env,
                          cwd=str(ROOT), timeout=30)
    payload = json.loads(proc.stdout)
    # The lease must be visible in the payload whatever the exit code says.
    assert any(l.get("agent") == "peer_reviewer" for l in payload.get("leases") or []), payload
    r = shell("make build")
    assert r["permission"] == "allow", (
        f"hatch closed while a lease is listed; status rc={proc.returncode}", r)
    assert r["reason_code"] == "AV_ALLOW_LEASED_SHELL", r
