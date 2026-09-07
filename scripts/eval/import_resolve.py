#!/usr/bin/env python3
"""AST import resolver — catch hallucinated top-level imports (stdlib only).

Resolves literal ``import x`` / ``from x import y`` against:
  - Python standard library
  - local packages/modules under the workspace root
  - modules findable via importlib.util.find_spec (installed env)

Non-literal / dynamic imports are reported as warnings and do not fail the build.

    python3 scripts/eval/import_resolve.py scripts/coord aeap
    python3 scripts/eval/import_resolve.py --json path/to/file.py

Exit 0 if all literal top-level imports resolve; exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    "venv",
    ".venv",
    ".venv_quant",
    "node_modules",
    "templates",  # portable mirror — checked separately when gating template
}


def _stdlib() -> set[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    # Fallback for very old Pythons (should not hit on 3.11+)
    return {"sys", "os", "ast", "json", "re", "pathlib", "typing"}


STDLIB = _stdlib()


def local_roots(root: Path) -> set[str]:
    """Top-level importable names present as packages or modules in the repo."""
    found: set[str] = set()
    for child in root.iterdir():
        if child.name.startswith(".") or child.name in SKIP_DIR_NAMES:
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            found.add(child.name)
        elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
            found.add(child.stem)
    # scripts/ is not usually imported as top-level; still allow scripts.* if packaged
    scripts = root / "scripts"
    if scripts.is_dir():
        found.add("scripts")
    return found


def iter_py_files(paths: list[Path], root: Path) -> list[Path]:
    root = root.resolve()
    files: list[Path] = []
    for p in paths:
        p = p if p.is_absolute() else (root / p)
        p = p.resolve()
        if not p.exists():
            continue
        if p.is_file() and p.suffix == ".py":
            files.append(p)
            continue
        if p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in SKIP_DIR_NAMES for part in f.parts):
                    continue
                files.append(f.resolve())
    return sorted(set(files))


def extract_imports(tree: ast.AST) -> tuple[set[str], list[str]]:
    """Return (literal_top_level_modules, dynamic_warnings)."""
    literal: set[str] = set()
    warnings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top:
                    literal.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                # relative: from . import x — local, skip top-level resolve
                continue
            if node.level and node.level > 0:
                continue
            top = node.module.split(".", 1)[0]
            if top:
                literal.add(top)
        elif isinstance(node, ast.Call):
            # importlib.import_module("x") with non-constant → warn
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr == "import_module":
                name = "import_module"
            elif isinstance(func, ast.Name) and func.id == "__import__":
                name = "__import__"
            if name and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    top = arg0.value.split(".", 1)[0]
                    if top:
                        literal.add(top)
                else:
                    warnings.append(f"dynamic {name}(...) — not statically resolved")
    return literal, warnings


def resolve_name(name: str, root: Path, local: set[str], importer: Path | None = None) -> bool:
    if name in STDLIB or name in local:
        return True
    # Local path: name/ as package or name.py at repo root
    if (root / name).is_dir() or (root / f"{name}.py").is_file():
        return True
    # Same-directory / pytest sibling (e.g. ``import conftest``)
    if importer is not None:
        sib = importer.parent / f"{name}.py"
        if sib.is_file():
            return True
        if (importer.parent / name / "__init__.py").is_file():
            return True
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
    return spec is not None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def check_files(files: list[Path], root: Path) -> dict:
    root = root.resolve()
    local = local_roots(root)
    unresolved: list[dict] = []
    warnings: list[str] = []
    checked = 0
    for f in files:
        f = f.resolve()
        try:
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(f))
        except SyntaxError as e:
            unresolved.append(
                {"file": _rel(f, root), "module": "<syntax>", "detail": str(e)}
            )
            continue
        except UnicodeDecodeError:
            continue
        mods, warns = extract_imports(tree)
        for w in warns:
            warnings.append(f"{_rel(f, root)}: {w}")
        for mod in sorted(mods):
            checked += 1
            if not resolve_name(mod, root, local, importer=f):
                unresolved.append(
                    {"file": _rel(f, root), "module": mod, "detail": "unresolved"}
                )
    return {
        "ok": len(unresolved) == 0,
        "files": len(files),
        "imports_checked": checked,
        "unresolved": unresolved,
        "warnings": warnings[:50],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*", default=["scripts/coord", "aeap"], help="Files or dirs")
    ap.add_argument("--root", default=None, help="Workspace root (default: repo root)")
    ap.add_argument("--json", action="store_true", help="Machine-readable report")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else ROOT
    files = iter_py_files([Path(p) for p in args.paths], root)
    report = check_files(files, root)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"import_resolve: files={report['files']} imports={report['imports_checked']} "
            f"unresolved={len(report['unresolved'])}"
        )
        for u in report["unresolved"][:40]:
            print(f"  UNRESOLVED {u['file']}: {u['module']} ({u['detail']})")
        for w in report["warnings"][:10]:
            print(f"  warn: {w}")
        print("PASS" if report["ok"] else "FAIL")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
