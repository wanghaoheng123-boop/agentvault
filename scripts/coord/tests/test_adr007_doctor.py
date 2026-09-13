"""ADR-007: ghost lease FAIL + CURRENT word-budget WARN."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

TZ = timezone(timedelta(hours=8))

GATES_MIN = """# EXECUTION GATES

## Elevate
x

## Done means exit 0
gate

## Handoff
checkpoint

## Monotonic rigor
never weaken
"""


def _ensure_doctor_prereqs(av):
    (av.COORD / "PROTOCOL.md").write_text("# PROTOCOL\n", encoding="utf-8")
    if not (av.COORD / "EXECUTION_GATES.md").exists():
        (av.COORD / "EXECUTION_GATES.md").write_text(GATES_MIN, encoding="utf-8")
    if not av.BOARD.exists():
        av.BOARD.write_text("# board\n", encoding="utf-8")


def test_doctor_fails_on_ghost_lease_missing_path(av):
    _ensure_doctor_prereqs(av)
    ghost = "MemoryBank/coord/DOES_NOT_EXIST_GHOST.md"
    assert not (av.ROOT / ghost).exists()
    lease = {
        "schema_version": "1.1",
        "agent_id": "orchestrator",
        "resources": [ghost],
        "expires_at": (datetime.now(TZ) + timedelta(hours=1)).isoformat(),
        "created_at": datetime.now(TZ).isoformat(),
    }
    lp = av.lease_path(ghost)
    lp.write_text(json.dumps(lease), encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = av.cmd_doctor(SimpleNamespace())
    out = buf.getvalue()
    assert rc == 1
    assert "GHOST_LEASE" in out
    assert "DOES_NOT_EXIST_GHOST" in out


def test_doctor_warns_on_current_word_budget(av, capsys):
    _ensure_doctor_prereqs(av)
    pad = " ".join(["word"] * 120)
    text = av.CURRENT.read_text(encoding="utf-8")
    av.CURRENT.write_text(text + "\n\n" + pad + "\n", encoding="utf-8")

    rc = av.cmd_doctor(SimpleNamespace())
    captured = capsys.readouterr().out
    assert rc == 0  # WARN only
    assert "CURRENT_WORD_BUDGET" in captured
