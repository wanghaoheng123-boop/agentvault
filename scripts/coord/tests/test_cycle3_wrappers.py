"""Thin wrappers: verify.sh → gate; check_imports → import_resolve."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
VERIFY_SH = REPO / "scripts" / "verify.sh"
CHECK_IMPORTS = REPO / "scripts" / "guard" / "check_imports.py"
RESOLVER = REPO / "scripts" / "eval" / "import_resolve.py"


def test_verify_sh_is_gate_wrapper():
    text = VERIFY_SH.read_text(encoding="utf-8")
    assert "bin/avcoord" in text and "gate" in text
    assert "verify" in text.lower()  # documents verify≠gate
    assert "NOT" in text or "not" in text
    # Must not exec avcoord verify as the DONE path
    assert 'avcoord" verify' not in text
    assert "avcoord verify" not in text.split("exec", 1)[-1]


def test_check_imports_aliases_import_resolve(tmp_path):
    phantom = "totally_fake_pkg_" + "alias777"
    f = tmp_path / "x.py"
    f.write_text(f"import {phantom}\n", encoding="utf-8")
    via_alias = subprocess.run(
        [sys.executable, str(CHECK_IMPORTS), "--root", str(tmp_path), str(f)],
        capture_output=True,
        text=True,
    )
    via_engine = subprocess.run(
        [sys.executable, str(RESOLVER), "--root", str(tmp_path), str(f)],
        capture_output=True,
        text=True,
    )
    assert via_alias.returncode == 1
    assert via_engine.returncode == 1
    assert via_alias.returncode == via_engine.returncode
