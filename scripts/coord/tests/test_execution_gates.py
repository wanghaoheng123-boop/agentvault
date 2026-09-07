"""Doctor / gates checks for EXECUTION_GATES.md (ADR-005)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
AV = REPO / "scripts" / "coord" / "avcoord.py"
GATES = REPO / "MemoryBank" / "coord" / "EXECUTION_GATES.md"

REQUIRED_HEADINGS = ("Elevate", "Done means exit 0", "Handoff", "Monotonic rigor")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def av(tmp_path):
    mod = _load(AV, "avcoord_under_test_gates")
    sb = tmp_path / "sb"
    mod._init_sandbox(sb)
    yield mod
    mod.configure_paths(REPO)


def test_repo_gates_has_required_sections():
    assert GATES.is_file()
    text = GATES.read_text(encoding="utf-8")
    for h in REQUIRED_HEADINGS:
        assert h in text, f"missing: {h}"
    assert "avcoord gate" in text or "bin/avcoord gate" in text
    assert "MemoryBank/active/" in text


def test_doctor_fails_when_gates_missing(av):
    gates = av.COORD / "EXECUTION_GATES.md"
    gates.unlink()

    class NS:
        pass

    assert av.cmd_doctor(NS()) == 1


def test_doctor_fails_when_gates_section_missing(av):
    (av.COORD / "EXECUTION_GATES.md").write_text("# stub\n", encoding="utf-8")

    class NS:
        pass

    assert av.cmd_doctor(NS()) == 1


def test_doctor_fails_on_memorybank_active(av):
    (av.MB / "active").mkdir(parents=True)

    class NS:
        pass

    assert av.cmd_doctor(NS()) == 1


def test_doctor_passes_with_sandbox_gates(av):
    class NS:
        pass

    assert av.cmd_doctor(NS()) == 0
