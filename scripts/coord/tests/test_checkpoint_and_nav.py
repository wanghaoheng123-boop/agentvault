"""Tests for checkpoint redaction and nav link checker.

Forbidden-literal note: do not embed home-directory path literals or GitHub
token prefixes in this source file — scripts/coord/tests is mirrored into the
portable template and sync_template.py fails the build on those patterns.
Build probe strings at runtime by concatenation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
AV = REPO / "scripts" / "coord" / "avcoord.py"
NAV = REPO / "scripts" / "nav" / "check_links.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def av(tmp_path, monkeypatch):
    mod = _load(AV, "avcoord_under_test_checkpoint")
    sb = tmp_path / "sb"
    mod._init_sandbox(sb)
    # Stamp relative to now: a fixed date turns every freshness assertion into a time bomb
    # that fails on a clock tick rather than on a code change.
    fresh = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=1)).isoformat(timespec="seconds")
    (sb / "MemoryBank" / "CURRENT.md").write_text(
        "---\nsession_id: sess-test\nstale_after_hours: 48\n"
        f"last_updated: {fresh}\n---\n# CURRENT\n",
        encoding="utf-8",
    )
    yield mod
    mod.configure_paths(REPO)


def test_redact_secrets_strips_tokens(av):
    home = "/" + "Users/" + "alice"
    token = "ghp_" + ("a" * 36)
    sk = "sk-" + ("b" * 20)
    raw = f"token={token} password=hunter2 {home}/secret {sk}"
    out = av._redact_secrets(raw)
    assert token not in out
    assert "hunter2" not in out
    assert home not in out
    assert sk not in out
    assert "REDACTED" in out


def test_checkpoint_writes_handoff(av):
    home = "/" + "Users/" + "bob"

    class NS:
        agent = "orchestrator"
        notes = ""
        goal = f"finish API_KEY=supersecret123 near {home}/proj"
        status = "gate pending"
        file = f"{home}/proj/foo.py"
        line = 42
        blockers = "none"
        remaining = ""
        next = "run bin/avcoord gate"

    rc = av.cmd_checkpoint(NS())
    assert rc == 0
    sessions = av.MB / "sessions"
    handoffs = list(sessions.glob("handoff-*.md"))
    assert handoffs, "expected a handoff file"
    text = handoffs[0].read_text(encoding="utf-8")
    assert "supersecret123" not in text
    assert home not in text
    assert "sess-test" in text
    assert "## 1. Active Goal" in text
    assert "## 4. Exact Next Shell/Edit" in text
    assert "four_vector_v1" in text


def test_nav_check_passes_on_repo():
    if not (REPO / "WORKSPACE_INDEX.md").exists():
        pytest.skip("lab WORKSPACE_INDEX.md not present in portable template")
    nav_path = REPO / "scripts" / "nav" / "check_links.py"
    if not nav_path.exists():
        pytest.skip("scripts/nav/check_links.py not shipped in this tree")
    nav = _load(nav_path, "nav_check_under_test")
    report = nav.check(REPO)
    assert report["ok"], json.dumps(report, indent=2)
    assert not report["missing_mentions"]
    assert report["hop1"]["total"] > 0
