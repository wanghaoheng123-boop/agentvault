#!/usr/bin/env python3
"""Thin alias → scripts/eval/import_resolve.py (ADR-006).

Do not duplicate the AST resolve engine here. Prefer calling import_resolve
directly; this path exists only for muscle-memory / external check_imports names.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "import_resolve.py"

if __name__ == "__main__":
    if not _TARGET.is_file():
        print(f"FAIL: missing engine {_TARGET}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(subprocess.call([sys.executable, str(_TARGET), *sys.argv[1:]]))
