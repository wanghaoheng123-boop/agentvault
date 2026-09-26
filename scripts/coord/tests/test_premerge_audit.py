"""The pre-merge audit must be able to approve content-only branches without weakening S1.

Before this file the audit had no tests. It sent a full patch only for the
invariant-bearing CODE_PATHS and a bare `--stat` for everything else, so the
model reviewer could never evaluate S1 ("no credential in added lines") for
content paths such as reports/. On 2026-09-25 three runs against a
content-only branch gave a non-verdict or BLOCKED and never APPROVED.

The fix is a deterministic local credential scan over every added line of
`git diff --text TARGET...HEAD`, run before the model and failing closed, with
its redacted summary included in the payload.

Test secrets are assembled at runtime by concatenation, so this source file
never contains a literal that the scanner (or any other secret scanner) would
match; otherwise the branch that adds these tests would fail its own audit.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
SCRIPT = SRC / "premerge_audit.sh"
SCANNER = SRC / "credential_scan.py"

# Runtime-assembled fixtures (see module docstring).
FAKE_ANTHROPIC = "sk-" + "ant-" + "api03-" + "Q7" * 18
FAKE_AWS = "AK" + "IA" + "Z3XQ7PLM2NB8VC4K"
FAKE_GITHUB = "gh" + "p_" + "a1B2c3D4" * 5
FAKE_PASSWORD = "Tr0ub4dor" + "&3-horse"  # credential-scan: allow (fixture assembled at runtime)
FAKE_RUN_TOKEN = "rt_" + "9f8e7d6c5b4a" * 3
PRIVATE_KEY_LINE = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"  # credential-scan: allow (fixture assembled at runtime)
BITCOIN_ROW = ("| Bitcoin | What will marginal buyers pay for a scarce digital token? "
               "| Risk appetite, liquidity | Individuals, funds, some corporates | 24 hours, 7 days |")


# ---------------------------------------------------------------- scanner unit tests

def _scanner():
    spec = importlib.util.spec_from_file_location("credential_scan_under_test", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["credential_scan_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _diff(path: str, *added: str, removed: tuple[str, ...] = ()) -> str:
    lines = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null",
             f"+++ b/{path}", f"@@ -0,0 +1,{len(added)} @@"]
    lines += [f"-{r}" for r in removed]
    lines += [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("line,pattern", [
    (f'api_key = "{FAKE_ANTHROPIC}"', "anthropic_openai_key"),
    (f"aws_access_key_id={FAKE_AWS}", "aws_access_key_id"),
    (f"export GITHUB_TOKEN={FAKE_GITHUB}", "github_token"),
    (PRIVATE_KEY_LINE, "private_key_block"),
    (f"Authorization: Bearer {FAKE_RUN_TOKEN}", "bearer_token"),
    (f"https://api.example.com/v1/data?api_key={FAKE_RUN_TOKEN}&x=1", "url_credential_param"),
    (f"db_password: {FAKE_PASSWORD}", "credential_assignment"),
    (f'  "run_token": "{FAKE_RUN_TOKEN}",', "credential_assignment"),
])
def test_planted_credentials_in_reports_are_caught(line, pattern):
    res = _scanner().scan_diff(_diff("reports/pkg/notes.md", "context", line))
    assert res["hit_count"] >= 1, f"planted {pattern} was not flagged"
    assert pattern in {h["pattern"] for h in res["hits"]}
    hit = res["hits"][0]
    assert hit["path"] == "reports/pkg/notes.md" and hit["line"] == 2


@pytest.mark.parametrize("line", [
    BITCOIN_ROW,
    "Tokens of appreciation were exchanged; the secret sauce is patience.",
    "Use a password manager and never reuse a password.",
    "The token bucket refills at a fixed rate.",
    "token: <your-token>",
    "api_key = ${FRED_API_KEY}",
    "password = os.environ.get(",
    "token = args.run_token",
    '"token_sha256": "' + "e3b0c44298fc1c149afbf4c8996fb924" * 2 + '"',
    "secret: REDACTED",
    "var TEChartsToken = '[REDACTED]'; var TEObfuscationkey = 'x';",
    "            auth: { token: '[REDACTED-JWT]' },",
    'line = f"db_password: {FAKE_PASSWORD}{i:02d}"',
    'url = f"https://api.example.com/v1?api_key={API_TOKEN}&x=1"',
    "export API_KEY=${{ secrets.API_KEY }}",
])
def test_prose_placeholders_and_code_references_are_not_flagged(line):
    res = _scanner().scan_diff(_diff("reports/pkg/monograph.md", line))
    assert res["hit_count"] == 0, f"false positive on: {line!r} -> {res['hits']}"


def test_removed_lines_are_not_scanned():
    res = _scanner().scan_diff(_diff("reports/x.md", "harmless", removed=(f'api_key = "{FAKE_ANTHROPIC}"',)))
    assert res["hit_count"] == 0


def test_findings_are_redacted():
    res = _scanner().scan_diff(_diff("reports/x.md", f'api_key = "{FAKE_ANTHROPIC}"'))
    blob = json.dumps(res)
    assert FAKE_ANTHROPIC not in blob, "the raw secret leaked into the scan summary"
    assert res["hits"][0]["length"] >= 20 and len(res["hits"][0]["preview"]) <= 8


def test_line_numbers_follow_hunk_headers():
    diff = ("diff --git a/reports/y.md b/reports/y.md\n--- a/reports/y.md\n+++ b/reports/y.md\n"
            "@@ -10,0 +41,2 @@\n+fine\n" + f"+db_password: {FAKE_PASSWORD}\n")
    hit = _scanner().scan_diff(diff)["hits"][0]
    assert (hit["path"], hit["line"]) == ("reports/y.md", 42)


def test_inline_pragma_exempts_a_line_only_on_reviewed_paths():
    line = f'api_key = "{FAKE_ANTHROPIC}"  # credential-scan: allow (test fixture)'
    mod = _scanner()
    assert mod.scan_diff(_diff("scripts/x.py", line), pragma_paths=("scripts",))["hit_count"] == 0


@pytest.mark.parametrize("path", ["reports/pkg/notes.md", "docs/notes/x.md", "scriptsX/y.py"])
def test_pragma_is_ignored_where_the_reviewer_sees_only_a_stat(path):
    # Found by the audit reviewing this very change: a pragma honoured on every path let a
    # credential on a --stat-only path pass the scan unseen.
    line = f'api_key = "{FAKE_ANTHROPIC}"  # credential-scan: allow'
    mod = _scanner()
    assert mod.scan_diff(_diff(path, line), pragma_paths=("scripts",))["hit_count"] >= 1
    assert mod.scan_diff(_diff(path, line))["hit_count"] >= 1, "default must honour the pragma nowhere"


def test_allowlist_matches_path_pattern_and_value_hash(tmp_path):
    mod = _scanner()
    diff = _diff("reports/raw/page.html", f"db_password: {FAKE_PASSWORD}")
    first = mod.scan_diff(diff)["hits"][0]
    allow = [{"path": "reports/raw/page.html", "pattern": first["pattern"], "sha256": first["sha256"],
              "reason": "test"}]
    assert mod.scan_diff(diff, allowlist=allow)["hit_count"] == 0
    other = _diff("reports/raw/page.html", f"db_password: {FAKE_PASSWORD}X")
    assert mod.scan_diff(other, allowlist=allow)["hit_count"] == 1, "allowlist must pin the exact value"


def test_listed_findings_are_capped_but_counted():
    lines = [f"db_password: {FAKE_PASSWORD}{i:02d}" for i in range(30)]
    res = _scanner().scan_diff(_diff("reports/z.md", *lines), max_listed=5)
    assert res["hit_count"] == 30 and len(res["hits"]) == 5


def test_cli_exit_codes(tmp_path):
    clean = subprocess.run([sys.executable, str(SCANNER)], input=_diff("reports/a.md", BITCOIN_ROW),
                           text=True, capture_output=True)
    dirty = subprocess.run([sys.executable, str(SCANNER)], input=_diff("reports/a.md", f"db_password: {FAKE_PASSWORD}"),
                           text=True, capture_output=True)
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")
    broken = subprocess.run([sys.executable, str(SCANNER), "--allowlist", str(bad)],
                            input=_diff("reports/a.md", "x"), text=True, capture_output=True)
    assert (clean.returncode, dirty.returncode, broken.returncode) == (0, 1, 2)
    assert json.loads(clean.stdout)["hit_count"] == 0
    assert FAKE_PASSWORD not in dirty.stdout + dirty.stderr


# ---------------------------------------------------------------- end-to-end: the real script

def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway repo: main has the audit machinery; branch `feature` adds `files`."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "coord").mkdir(parents=True)
    (repo / ".agentvault" / "bin").mkdir(parents=True)
    (repo / ".agentvault" / "invariants").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts/coord/premerge_audit.sh")
    if SCANNER.exists():
        shutil.copy2(SCANNER, repo / "scripts/coord/credential_scan.py")
    shutil.copy2(ROOT / ".agentvault/bin/av-board.py", repo / ".agentvault/bin/av-board.py")
    for name in ("security.md", "concurrency-and-data.md"):
        shutil.copy2(ROOT / ".agentvault/invariants" / name, repo / ".agentvault/invariants" / name)
    (repo / "README.md").write_text("fixture\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=fixture", "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=repo, check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "content"], cwd=repo, check=True)
    return repo


def _fake_claude(tmp_path: Path) -> Path:
    """A stand-in reviewer that records how it was called."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    rec = tmp_path / "rec"
    rec.mkdir()
    fake = bindir / "claude"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'REC="{rec}"\n'
        'touch "$REC/invoked"\n'
        'pwd -P > "$REC/cwd"\n'
        'printf "%s\\n" "$@" > "$REC/args"\n'
        'cat > "$REC/stdin"\n'
        'printf "%s" "${FAKE_VERDICT:-APPROVED}"\n'
        'exit "${FAKE_RC:-0}"\n')
    fake.chmod(0o755)
    return bindir


def _run(repo: Path, bindir: Path, **env_overrides):
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env.pop("AV_AUDIT_MAX_BYTES", None)
    env.update(env_overrides)
    return subprocess.run(["bash", "scripts/coord/premerge_audit.sh", "main"], cwd=repo, env=env,
                          text=True, capture_output=True, timeout=120)


def _rec(tmp_path: Path, name: str) -> Path:
    return tmp_path / "rec" / name


def test_planted_credential_in_reports_file_is_rejected_before_the_model(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/notes.md": f'# notes\napi_key = "{FAKE_ANTHROPIC}"\n'})
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir)
    assert r.returncode != 0, "a planted credential in reports/ passed the pre-merge audit"
    assert "REJECTED" in r.stdout and "credential" in r.stdout.lower()
    assert not _rec(tmp_path, "invoked").exists(), "the model was called despite a local scan hit"
    assert FAKE_ANTHROPIC not in r.stdout + r.stderr, "the audit printed the raw secret"


def test_clean_content_only_branch_can_be_approved(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": f"# Monograph\n\n{BITCOIN_ROW}\n"})
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir)
    assert r.returncode == 0, f"clean content-only branch was not approvable: {r.stdout}{r.stderr}"
    payload = _rec(tmp_path, "stdin").read_text()
    assert "DETERMINISTIC CREDENTIAL SCAN" in payload
    assert '"hit_count": 0' in payload
    assert "Reply with exactly APPROVED" in payload.splitlines()[-1] or \
        "Reply with exactly APPROVED" in payload[-400:], "instruction must close the payload"


def test_reviewer_runs_isolated_from_project_context(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": BITCOIN_ROW + "\n"})
    bindir = _fake_claude(tmp_path)
    assert _run(repo, bindir).returncode == 0
    cwd = Path(_rec(tmp_path, "cwd").read_text().strip())
    assert cwd != repo.resolve() and repo.resolve() not in cwd.parents, (
        "the reviewer ran inside the repo, where project CLAUDE.md context derails it")
    args = _rec(tmp_path, "args").read_text().splitlines()
    assert "--print" in args and "--system-prompt" in args and "--safe-mode" in args


def test_oversized_payload_still_rejects(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": BITCOIN_ROW + "\n"})
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir, AV_AUDIT_MAX_BYTES="100")
    assert r.returncode != 0 and "REJECTED: audit payload" in r.stdout
    assert not _rec(tmp_path, "invoked").exists()


def test_verdict_must_be_exactly_approved(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": BITCOIN_ROW + "\n"})
    bindir = _fake_claude(tmp_path)
    assert _run(repo, bindir, FAKE_VERDICT="APPROVED with minor notes").returncode != 0


def test_reviewer_failure_is_not_an_approval(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": BITCOIN_ROW + "\n"})
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir, FAKE_RC="1")
    assert r.returncode != 0 and "AUDIT COULD NOT RUN" in r.stdout


def test_scanner_error_is_could_not_run_not_approval(tmp_path):
    repo = _repo(tmp_path, {"reports/pkg/monograph.md": BITCOIN_ROW + "\n"})
    (repo / "scripts/coord/credential_scan_allowlist.json").write_text("{not json")
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir)
    assert r.returncode != 0 and "AUDIT COULD NOT RUN" in r.stdout
    assert not _rec(tmp_path, "invoked").exists()


def test_pragma_on_a_content_path_does_not_pass_the_audit(tmp_path):
    body = f'# notes\napi_key = "{FAKE_ANTHROPIC}"  <!-- credential-scan: allow -->\n'
    repo = _repo(tmp_path, {"reports/pkg/notes.md": body})
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir)
    assert r.returncode != 0 and "REJECTED" in r.stdout
    assert not _rec(tmp_path, "invoked").exists()


def test_environment_cannot_substitute_an_unreviewed_allowlist(tmp_path):
    """An allowlist outside the repo is never seen by the reviewer; the env must not select one."""
    repo = _repo(tmp_path, {"reports/pkg/notes.md": f'api_key = "{FAKE_ANTHROPIC}"\n'})
    mod = _scanner()
    hits = mod.scan_diff(_diff("reports/pkg/notes.md", f'api_key = "{FAKE_ANTHROPIC}"'))["hits"]
    rogue = tmp_path / "rogue.json"
    rogue.write_text(json.dumps([{"path": h["path"], "pattern": h["pattern"], "sha256": h["sha256"],
                                  "reason": "rogue"} for h in hits]))
    bindir = _fake_claude(tmp_path)
    r = _run(repo, bindir, AV_CREDSCAN_ALLOWLIST=str(rogue))
    assert r.returncode != 0 and "REJECTED" in r.stdout, "an env-selected allowlist bypassed the scan"
    assert not _rec(tmp_path, "invoked").exists()
