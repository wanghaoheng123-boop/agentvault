"""Isolated fixtures for the avcoord contract tests.

Isolation is fiddly for three reasons, each handled explicitly below:
  * `configure_paths()` with no argument re-reads AVCOORD_ROOT, and main() calls it on
    every invocation — so the env var must be set as well as the explicit call.
  * teardown must restore paths explicitly; relying on monkeypatch's undo ordering can
    leave the module bound to the real repo.
  * `avcoord._init_sandbox` is NOT reused: it rmtree's and mutates os.environ globally,
    which would defeat monkeypatch teardown.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1]


def _load_avcoord():
    spec = importlib.util.spec_from_file_location("avcoord_under_test", SRC / "avcoord.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["avcoord_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


REGISTRY = {
    "schema_version": "1.0",
    "agents": [
        {"id": "orchestrator", "persona": "Master_Orchestrator", "alias": "@Orchestrator",
         "write_scopes": ["MemoryBank/**", "scripts/**", "docs/**"], "status": "active"},
        {"id": "code_generator", "persona": "Code-Generator", "alias": "@Code-Generator",
         "write_scopes": ["scripts/**"], "status": "active"},
        {"id": "doc_writer", "persona": "Doc-Writer", "alias": "@Doc-Writer",
         "write_scopes": ["reports/**"], "status": "active"},
    ],
}

CONTESTED = {
    "schema_version": "1.0",
    "prefixes": [
        "MemoryBank/CURRENT.md",
        "MemoryBank/coord/next_ids.json",
        "OpenViking/",
        "AGENTS.md",
        "docs/manuscript/",
    ],
    "bare_names": ["CURRENT.md", "next_ids.json", "AGENTS.md", "index.json"],
}

CURRENT_MD = """---
version: 1.0
last_updated: 2026-09-06T21:00:00+08:00
session_id: sess-test
stale_after_hours: 48
---

# CURRENT

## Active ETS
- **Epic:** Test epic
- **SubTask:** IN PROGRESS
"""


def _scaffold(root: Path) -> None:
    coord = root / "MemoryBank" / "coord"
    (coord / "leases").mkdir(parents=True, exist_ok=True)
    for role in ("orchestrator", "code_generator", "doc_writer"):
        for box in ("tmp", "new", "cur", "done"):
            (coord / "mail" / role / box).mkdir(parents=True, exist_ok=True)
    (root / "MemoryBank" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "MemoryBank" / "agents" / "registry.json").write_text(json.dumps(REGISTRY, indent=2))
    (coord / "contested.json").write_text(json.dumps(CONTESTED, indent=2))
    (coord / "next_ids.json").write_text(json.dumps({"progress": 1, "episode": 1, "message": 1}))
    (coord / "threads.json").write_text(json.dumps({"schema_version": "1.0", "threads": []}))
    (coord / "audit.jsonl").touch()
    (root / "MemoryBank" / "CURRENT.md").write_text(CURRENT_MD)
    (root / "AGENTS.md").write_text("# AGENTS\n")
    (root / "OpenViking").mkdir(exist_ok=True)
    (root / "OpenViking" / "L0_Core_Directives.md").write_text("# L0\n")
    (root / "docs" / "manuscript").mkdir(parents=True, exist_ok=True)
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "notindex.json").write_text("{}")
    (root / "scripts" / "coord").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "coord" / "avcoord.py").write_text("# stub\n")
    (root / "EpisodicTracker").mkdir(exist_ok=True)
    (root / "EpisodicTracker" / "events.jsonl").touch()


@pytest.fixture
def av(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    _scaffold(root)
    mod = _load_avcoord()
    monkeypatch.setenv("AVCOORD_ROOT", str(root))
    original = mod.ROOT
    mod.configure_paths(root)
    yield mod
    mod.set_clock(None)
    mod.configure_paths(original)


@pytest.fixture
def clock(av, monkeypatch):
    """Deterministic clock, also exported to subprocesses via AVCOORD_FAKE_NOW."""
    state = {"t": datetime(2026, 9, 6, 21, 0, tzinfo=av.TZ)}
    av.set_clock(lambda: state["t"])

    def advance(**kw):
        state["t"] += timedelta(**kw)
        return state["t"]

    return SimpleNamespace(advance=advance, get=lambda: state["t"])


def ns(**kw):
    """argparse.Namespace stand-in; commands read optional flags via getattr."""
    return SimpleNamespace(**kw)


def start_run(av, role, runtime="pytest", task=""):
    """Register a real run and return its id. --run is authenticated, not a free string."""
    import io, contextlib, json as _json
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = av.cmd_run_start(ns(role=role, runtime=runtime, task=task, ttl_hours=1))
    assert rc == 0
    return _json.loads(buf.getvalue())["run_id"]


def claim(av, agent, resource, ttl="15m", **kw):
    return av.cmd_claim(
        ns(agent=agent, resource=[resource] if isinstance(resource, str) else resource,
           ttl=ttl, reason=kw.pop("reason", "test"), task_id=kw.pop("task_id", ""), **kw)
    )
