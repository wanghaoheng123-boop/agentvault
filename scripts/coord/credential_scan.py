#!/usr/bin/env python3
"""Deterministic S1 credential scan over the added lines of a unified diff.

Why this exists: premerge_audit.sh sends the model reviewer a full patch only
for the invariant-bearing CODE_PATHS; every other path arrives as `--stat`.
The reviewer therefore cannot evaluate S1 ("no credential in added lines") for
content paths such as reports/ and blocked every content-only branch by
construction. This scanner reads `git diff --text -U0 TARGET...HEAD` on stdin,
checks every added line on every path, and reports a redacted summary that the
audit fails closed on and includes in the reviewer's payload.

Exit codes: 0 = no findings, 1 = findings, 2 = scanner error (never an approval).
Findings are redacted: pattern id, path:line, the first four characters of the
match, its length and its SHA-256. The raw value is never printed.

Exemptions, both only where a reviewer can see them:
  * an inline `credential-scan: allow` on the same added line, honoured ONLY on
    paths passed as --pragma-path (premerge_audit.sh passes its CODE_PATHS, the
    surface sent to the reviewer as a full patch). On a --stat-only path a pragma
    is ignored: there the reviewer would never see the line it exempts.
  * the allowlist file credential_scan_allowlist.json beside this file (itself on
    the full-patch surface), whose entries pin path + pattern + sha256 of the
    exact matched value. Deliberately NOT selectable through the environment: an
    allowlist outside the repo would exempt findings the reviewer never sees.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PRAGMA = "credential-scan: allow"
DEFAULT_ALLOWLIST = Path(__file__).resolve().with_name("credential_scan_allowlist.json")

# High-signal formats: the match itself is the secret.
FORMAT_PATTERNS = [
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("aws_access_key_id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("anthropic_openai_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)(?<![A-Za-z0-9])bearer\s+([A-Za-z0-9._~+/-]{20,}=*)")),
    ("basic_auth_url", re.compile(r"(?i)(?<![A-Za-z0-9])[a-z][a-z0-9+.-]*://[^/\s:@\"'<>]+:([^/\s:@\"'<>]{3,})@")),
    ("url_credential_param", re.compile(
        r"(?i)[?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|key)"
        r"=([^&\s\"'<>#]{8,})")),
]

# Key-value shape: a credential-like name assigned a literal value.
ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|credential|private[_-]?key"
    r"|access[_-]?key)[A-Za-z0-9_.-]*)"
    r"[\"']?\s*[:=]\s*(?P<quote>[\"'])?(?P<value>[^\s\"'<>,;`]{8,})")
HASHLIKE_KEY = re.compile(r"(?i)(sha\d*|hash|digest|fingerprint|checksum)")
PLACEHOLDER = re.compile(
    r"(?i)^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\*+|[x.]+|\[?redacted[A-Za-z0-9_-]*\]?|changeme|placeholder|example|dummy|fake"
    r"|none|null|true|false|undefined|required|optional|string|your[_-][A-Za-z0-9_-]*)$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
TEMPLATE = re.compile(r"^\$?\{[^{}]*\}")        # f-string / shell / template interpolation, not a literal
CAPTURED = {"bearer_token", "basic_auth_url", "url_credential_param"}
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _is_benign_value(value: str, quoted: bool, key: str) -> bool:
    if HASHLIKE_KEY.search(key):
        return True                                   # a digest of a secret is not the secret
    if TEMPLATE.match(value) or PLACEHOLDER.match(value):
        return True
    if not quoted and ("(" in value or "[" in value or IDENTIFIER.match(value)):
        return True                                   # code reference, not a literal
    return False


def _finding(pattern: str, path: str, line: int, value: str) -> dict:
    return {"pattern": pattern, "path": path, "line": line, "preview": value[:4],
            "length": len(value), "sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()}


def scan_line(text: str, honour_pragma: bool = False) -> list[tuple[str, str]]:
    """(pattern id, matched secret value) pairs for one added line."""
    if honour_pragma and PRAGMA in text:
        return []
    hits: list[tuple[str, str]] = []
    for pid, rx in FORMAT_PATTERNS:
        for m in rx.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            if pid in CAPTURED and (TEMPLATE.match(value) or PLACEHOLDER.match(value)):
                continue
            hits.append((pid, value))
    for m in ASSIGNMENT.finditer(text):
        value = m.group("value")
        if not _is_benign_value(value, bool(m.group("quote")), m.group("key")):
            hits.append(("credential_assignment", value))
    return hits


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def scan_diff(diff_text: str, allowlist: list[dict] | None = None, max_listed: int = 20,
              pragma_paths: tuple[str, ...] = ()) -> dict:
    allowed = {(a["path"], a["pattern"], a["sha256"]) for a in (allowlist or [])}
    path, lineno = None, 0
    files: set[str] = set()
    added = 0
    hits: list[dict] = []
    total = suppressed = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            if path:
                files.add(path)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git "):
            continue
        m = HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if raw.startswith("+") and path is not None:
            added += 1
            for pid, value in scan_line(raw[1:], honour_pragma=_under(path, pragma_paths)):
                f = _finding(pid, path, lineno, value)
                if (path, pid, f["sha256"]) in allowed:
                    suppressed += 1
                    continue
                total += 1
                if len(hits) < max_listed:
                    hits.append(f)
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1                               # context line (only with -U>0)
    return {"scanner": "credential_scan v1", "files_scanned": len(files), "added_lines_scanned": added,
            "hit_count": total, "hits_listed": len(hits), "allowlisted": suppressed, "hits": hits,
            "patterns": [p for p, _ in FORMAT_PATTERNS] + ["credential_assignment"]}


def load_allowlist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data["entries"] if isinstance(data, dict) else data
    for e in entries:
        if not {"path", "pattern", "sha256", "reason"} <= set(e):
            raise ValueError(f"allowlist entry lacks path/pattern/sha256/reason: {e}")
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST), help="for tests; the audit never passes it")
    ap.add_argument("--max-listed", type=int, default=20)
    ap.add_argument("--pragma-path", action="append", default=[],
                    help="path prefix (repeatable) where the inline pragma is honoured")
    args = ap.parse_args(argv)
    try:
        allow = load_allowlist(Path(args.allowlist))
        diff = sys.stdin.buffer.read().decode("utf-8", "replace")
        result = scan_diff(diff, allowlist=allow, max_listed=args.max_listed,
                           pragma_paths=tuple(args.pragma_path))
    except Exception as exc:                          # fail closed, and say so
        print(json.dumps({"scanner": "credential_scan v1", "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(result, indent=1))
    return 1 if result["hit_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
