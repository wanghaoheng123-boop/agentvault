"""T02: execute the installer from the artifact a recipient actually unpacks.

All artifacts and installations live in tmp_path. The fake release has the REAL current
CLI and controlled seed/runtime files; release-builder acceptance separately exercises the
complete published file set. No nested templates directory or private lab is available.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "avcoord.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_release(root: Path, version="1.0.0", schema2=False):
    files, modes = {}, {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name == "RELEASE.json" or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        files[rel] = sha(p.read_bytes())
        modes[rel] = "0755" if p.stat().st_mode & 0o111 else "0644"
    manifest = {"schema_version": "2.0" if schema2 else "1.0", "version": version,
                "file_count": len(files), "files": files}
    if schema2:
        manifest["state_schema"] = "agentvault-v1"
        manifest["file_modes"] = modes
        payload = [{"path": p, "sha256": files[p], "mode": modes[p]} for p in files]
        manifest["payload_sha256"] = sha(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    (root / "RELEASE.json").write_text(json.dumps(manifest))
    return manifest


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "unpacked-release"
    files = {
        "scripts/coord/avcoord.py": CLI.read_bytes(),
        "scripts/runtime.py": b'print("release one")\n',
        "bin/avcoord": b'#!/bin/sh\nexec python3 -B "$(dirname "$0")/../scripts/coord/avcoord.py" "$@"\n',
        "AGENTS.md": b"# AgentVault\n",
        "CLAUDE.md": b"Follow AGENTS.md Boot exactly.\n",
        "README.md": b"# Public product\n",
        "MemoryBank/CURRENT.md": b"---\nlast_updated: 2000-01-01T00:00:00+00:00\n---\nOLD INSTANCE\n",
        "MemoryBank/projectbrief.md": b"# Set project brief\n",
        "MemoryBank/agents/registry.json": b'{"schema_version":"1.0","agents":[]}',
        "MemoryBank/coord/PROTOCOL.md": b"# Protocol\n",
        "MemoryBank/coord/EXECUTION_GATES.md": b"# Elevate\n# Done means exit 0\n# Handoff\n# Monotonic rigor\n",
        "MemoryBank/coord/next_ids.json": b'{"progress":999,"episode":999,"message":999}',
        "MemoryBank/coord/threads.json": b'{"threads":[{"id":"OLD_PRIVATE_TASK"}]}',
        "MemoryBank/coord/audit.jsonl": b'{"old":"history must not be seeded"}\n',
        "MemoryBank/coord/protocol.json": b'{"authority":"journal","epoch":88}',
        "EpisodicTracker/events.jsonl": b'{"old":"private event"}\n',
        "OpenViking/L0_Core_Directives.md": b"# Public rules\n",
        "GraphRAG/schema.json": b'{"nodes":[],"edges":[]}',
        "VectorRAG/index.json": b'{"references":[]}',
        "aeap/engine/__init__.py": b'"""Optional extension."""\n',
        "aeap/policies/gates.v1.yaml": b"calibration_status: UNCALIBRATED\nadmission_enabled: false\n",
        "requirements-core.txt": b"pytest>=8\n",
        "requirements-aeap.txt": b"numpy>=1.26\n",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (root / "bin/avcoord").chmod(0o755)
    write_release(root)
    cache = root / "scripts" / "__pycache__" / "private.cpython-311.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"runtime debris with an absolute private path")
    return root


def invoke(release, target, *flags, cwd=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("AVCOORD_ROOT", None)
    return subprocess.run([sys.executable, "-B", str(release / "scripts/coord/avcoord.py"),
                           "init", "--target", str(target), *flags], cwd=cwd or target.parent,
                          env=env, capture_output=True, text=True)


def snapshot(root):
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def test_standalone_core_boots_with_fresh_state_and_no_nested_template(release, tmp_path):
    target = tmp_path / "unrelated-project"
    assert not (release / "templates").exists()
    result = invoke(release, target)
    assert result.returncode == 0, result.stderr
    text = (target / "MemoryBank/CURRENT.md").read_text()
    assert "OLD INSTANCE" not in text
    stamp = next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("last_updated:"))
    assert abs((datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()) < 60
    assert json.loads((target / "MemoryBank/coord/next_ids.json").read_text())["message"] == 1
    assert json.loads((target / "MemoryBank/coord/threads.json").read_text())["threads"] == []
    assert json.loads((target / "MemoryBank/coord/protocol.json").read_text())["authority"] == "legacy"
    assert (target / "EpisodicTracker/events.jsonl").read_bytes() == b""
    assert not list(target.rglob("*.pyc")) and not list(target.rglob("__pycache__"))
    assert not (target / "aeap").exists() and not (target / "OpenViking").exists()
    assert not (target / "requirements-aeap.txt").exists()
    assert not (target / ".agentvault").exists()
    check = subprocess.run([sys.executable, "-B", str(target / "scripts/coord/avcoord.py"), "doctor"],
                           cwd=target, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert receipt["profiles"] == ["core"]
    assert all(not p.startswith(("MemoryBank/", "EpisodicTracker/")) for p in receipt["managed_files"])
    assert (target / "bin/avcoord").stat().st_mode & 0o111


@pytest.mark.parametrize("flags,full,aeap", [([], False, False), (["--full"], True, False),
                                           (["--with-aeap"], False, True),
                                           (["--full", "--with-aeap"], True, True)])
def test_profiles_are_explicit_and_repeat_is_byte_and_mtime_noop(release, tmp_path, flags, full, aeap):
    target = tmp_path / "project"
    assert invoke(release, target, *flags).returncode == 0
    assert (target / "OpenViking").exists() is full
    assert (target / "aeap").exists() is aeap
    if aeap:
        assert "admission_enabled: false" in (target / "aeap/policies/gates.v1.yaml").read_text()
    before = snapshot(target)
    result = invoke(release, target)  # Existing selections persist; no accidental profile downgrade.
    assert result.returncode == 0 and '"status": "unchanged"' in result.stdout
    assert snapshot(target) == before


def test_existing_memory_research_and_local_boot_are_never_overwritten(release, tmp_path):
    target = tmp_path / "existing"
    mine = {"MemoryBank/CURRENT.md": b"My exact current work\n", "MemoryBank/projectbrief.md": b"Private brief\n",
            "reports/findings.md": b"Research\n", "VectorRAG/index.json": b'{"private":true}',
            "AGENTS.md": b"My boot instructions\n"}
    for rel, data in mine.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    result = invoke(release, target, "--full")
    assert result.returncode == 0, result.stderr
    for rel, data in mine.items():
        assert (target / rel).read_bytes() == data
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert "AGENTS.md" not in receipt["managed_files"]


def test_preview_is_completely_write_free_for_missing_and_existing_targets(release, tmp_path):
    target = tmp_path / "never-created"
    result = invoke(release, target, "--preview", "--full", "--with-aeap")
    assert result.returncode == 0 and not target.exists()
    assert invoke(release, target).returncode == 0
    before = snapshot(target)
    assert invoke(release, target, "--dry-run", "--upgrade", "--full").returncode == 0
    assert snapshot(target) == before


def test_upgrade_changes_only_unmodified_managed_files_and_retains_recovery(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target, "--full", "--with-aeap").returncode == 0
    current = target / "MemoryBank/CURRENT.md"
    current.write_text("User current with unfinished work\n")
    policies = target / "aeap/policies/gates.v1.yaml"
    policies.write_text("My independently calibrated policy\n")
    original = (target / "scripts/runtime.py").read_bytes()
    (release / "scripts/runtime.py").write_text('print("release two")\n')
    (release / "MemoryBank/projectbrief.md").write_text("upstream seed changed\n")
    write_release(release, version="2.0.0")
    before = snapshot(target)
    assert invoke(release, target, "--upgrade", "--preview").returncode == 0
    assert snapshot(target) == before
    assert invoke(release, target, "--upgrade").returncode == 0
    assert (target / "scripts/runtime.py").read_text() == 'print("release two")\n'
    assert current.read_bytes() == before["MemoryBank/CURRENT.md"][0]
    assert policies.read_bytes() == before["aeap/policies/gates.v1.yaml"][0]
    assert (target / "MemoryBank/projectbrief.md").read_bytes() == before["MemoryBank/projectbrief.md"][0]
    backups = list((target / "AGENTVAULT_INSTALL_HISTORY").glob("*/before/scripts/runtime.py"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert receipt["release_version"] == "2.0.0" and receipt["previous_release_sha256"]


def test_upgrade_conflict_blocks_all_writes_including_new_files(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (target / "scripts/runtime.py").write_text("User modification\n")
    (release / "scripts/runtime.py").write_text("Upstream modification\n")
    (release / "scripts/new.py").write_text("New release file\n")
    write_release(release, "2.0.0")
    before = snapshot(target)
    result = invoke(release, target, "--upgrade")
    assert result.returncode == 1, result.stdout
    assert '"status": "conflict"' in result.stdout and "managed file changed locally" in result.stdout
    assert snapshot(target) == before
    assert not (target / "scripts/new.py").exists()
    assert not (target / "AGENTVAULT_INSTALL_PENDING.json").exists()


def test_source_update_requires_upgrade_and_force_never_overwrites(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (release / "scripts/runtime.py").write_text("Changed\n")
    write_release(release, "2.0.0")
    before = snapshot(target)
    assert invoke(release, target).returncode == 1
    forced = invoke(release, target, "--force")
    assert forced.returncode == 1 and "cannot overwrite user state" in forced.stderr
    assert snapshot(target) == before


@pytest.mark.parametrize("kind", ["target_root", "destination_dir", "destination_file", "source_file", "source_dir"])
def test_symlink_paths_fail_before_mutation(release, tmp_path, kind):
    target, outside = tmp_path / "project", tmp_path / "outside"
    outside.mkdir()
    (outside / "runtime.py").write_text("untouched\n")
    if kind == "target_root":
        target.symlink_to(outside, target_is_directory=True)
    elif kind == "destination_dir":
        target.mkdir()
        (target / "scripts").symlink_to(outside, target_is_directory=True)
    elif kind == "destination_file":
        (target / "scripts").mkdir(parents=True)
        (target / "scripts/runtime.py").symlink_to(outside / "runtime.py")
    elif kind == "source_file":
        (release / "scripts/runtime.py").unlink()
        (release / "scripts/runtime.py").symlink_to(outside / "runtime.py")
        write_release(release)
    else:
        code = release / "linked"
        code.symlink_to(outside, target_is_directory=True)
        manifest = json.loads((release / "RELEASE.json").read_text())
        manifest["files"]["linked/runtime.py"] = sha((outside / "runtime.py").read_bytes())
        manifest["file_count"] += 1
        (release / "RELEASE.json").write_text(json.dumps(manifest))
    before = snapshot(outside)
    result = invoke(release, target)
    assert result.returncode == 1 and "symlink" in result.stderr
    assert snapshot(outside) == before
    if not target.is_symlink():
        assert not (target / "AGENTVAULT_INSTALL.json").exists()


def test_documented_claude_alias_is_materialized_as_regular_adapter(release, tmp_path):
    (release / "CLAUDE.md").unlink()
    (release / "CLAUDE.md").symlink_to("AGENTS.md")
    write_release(release)
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    assert not (target / "CLAUDE.md").is_symlink()
    assert (target / "CLAUDE.md").read_bytes() == (release / "AGENTS.md").read_bytes()


@pytest.mark.parametrize("mutation", ["content", "traversal", "cache", "payload_digest"])
def test_corrupt_or_unsafe_release_is_rejected_before_target_creation(release, tmp_path, mutation):
    manifest = write_release(release, schema2=True)
    if mutation == "content":
        (release / "scripts/runtime.py").write_text("unmanifested change")
    elif mutation == "traversal":
        manifest["files"]["../escape"] = "0" * 64
        manifest["file_count"] += 1
    elif mutation == "cache":
        rel = "scripts/__pycache__/private.cpython-311.pyc"
        manifest["files"][rel] = sha((release / rel).read_bytes())
        manifest["file_count"] += 1
    else:
        manifest["payload_sha256"] = "0" * 64
    (release / "RELEASE.json").write_text(json.dumps(manifest))
    target = tmp_path / "never-created"
    result = invoke(release, target)
    assert result.returncode == 1
    assert not target.exists()


def test_schema2_release_modes_and_digest_install(release, tmp_path):
    write_release(release, schema2=True)
    target = tmp_path / "project"
    result = invoke(release, target)
    assert result.returncode == 0, result.stderr
    assert (target / "bin/avcoord").stat().st_mode & 0o111


def test_rollback_to_prior_release_preserves_newer_state(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    original = (release / "scripts/runtime.py").read_bytes()
    (release / "scripts/runtime.py").write_text("newer implementation\n")
    write_release(release, "2.0.0")
    assert invoke(release, target, "--upgrade").returncode == 0
    state = target / "MemoryBank/CURRENT.md"
    state.write_text("Work created after the upgrade\n")
    (release / "scripts/runtime.py").write_bytes(original)
    write_release(release, "1.0.0")
    assert invoke(release, target, "--upgrade").returncode == 0
    assert (target / "scripts/runtime.py").read_bytes() == original
    assert state.read_text() == "Work created after the upgrade\n"


def test_explicit_rollback_runs_from_installed_cli_without_release_source(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    original = (target / "scripts/runtime.py").read_bytes()
    (release / "scripts/runtime.py").write_text("version two\n")
    (release / "scripts/new_in_v2.py").write_text("new version helper\n")
    write_release(release, "2.0.0")
    assert invoke(release, target, "--upgrade").returncode == 0
    (target / "MemoryBank/CURRENT.md").write_text("Private state after upgrade\n")
    before = snapshot(target)
    # The installed CLI has neither RELEASE.json nor a nested template.
    result = invoke(target, target, "--rollback", "latest", "--preview")
    assert result.returncode == 0, result.stderr
    assert snapshot(target) == before
    result = invoke(target, target, "--rollback", "latest")
    assert result.returncode == 0, result.stderr
    assert (target / "scripts/runtime.py").read_bytes() == original
    assert not (target / "scripts/new_in_v2.py").exists()
    assert (target / "MemoryBank/CURRENT.md").read_text() == "Private state after upgrade\n"
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert receipt["release_version"] == "1.0.0"
    archived = list((target / "AGENTVAULT_INSTALL_HISTORY").glob("*/retired/scripts/new_in_v2.py"))
    assert len(archived) == 1 and archived[0].read_text() == "new version helper\n"
    # A second rollback can restore the retained v2 software image, including its new file.
    assert invoke(target, target, "--rollback", "latest").returncode == 0
    assert (target / "scripts/runtime.py").read_text() == "version two\n"
    assert (target / "scripts/new_in_v2.py").exists()
    assert (target / "MemoryBank/CURRENT.md").read_text() == "Private state after upgrade\n"


def test_rollback_rejects_any_managed_drift_before_writes(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (release / "scripts/runtime.py").write_text("version two\n")
    write_release(release, "2.0.0")
    assert invoke(release, target, "--upgrade").returncode == 0
    (target / "README.md").write_text("Local change in a different managed file\n")
    before = snapshot(target)
    result = invoke(target, target, "--rollback", "latest")
    assert result.returncode == 1 and "managed file changed locally" in result.stdout
    assert snapshot(target) == before


def test_declared_override_preserves_local_edit_through_upgrade_and_rollback(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (target / "AGENTS.md").write_text("Local project policy\n")
    (release / "scripts/runtime.py").write_text("version two\n")
    write_release(release, "2.0.0")
    result = invoke(release, target, "--upgrade", "--override", "AGENTS.md")
    assert result.returncode == 0, result.stderr + result.stdout
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert "AGENTS.md" in receipt["declared_overrides"]
    assert "AGENTS.md" not in receipt["managed_files"]
    assert invoke(target, target, "--rollback", "latest").returncode == 0
    assert (target / "AGENTS.md").read_text() == "Local project policy\n"
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    assert "AGENTS.md" in receipt["declared_overrides"]
    assert "AGENTS.md" not in receipt["managed_files"]


@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
def test_state_schema_mismatch_is_fail_closed(release, tmp_path, operation):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (release / "scripts/runtime.py").write_text("version two\n")
    write_release(release, "2.0.0")
    if operation == "upgrade":
        p = release / "RELEASE.json"
        obj = json.loads(p.read_text())
        obj["state_schema"] = "agentvault-v999"
        p.write_text(json.dumps(obj))
        args = ("--upgrade",)
    else:
        assert invoke(release, target, "--upgrade").returncode == 0
        p = target / "AGENTVAULT_INSTALL.json"
        obj = json.loads(p.read_text())
        obj["state_schema"] = "agentvault-v999"
        p.write_text(json.dumps(obj))
        args = ("--rollback", "latest")
    before = snapshot(target)
    result = invoke(release, target, *args)
    assert result.returncode == 1 and "schema" in result.stderr
    assert snapshot(target) == before


def test_rollback_before_image_corruption_is_rejected(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    (release / "scripts/runtime.py").write_text("version two\n")
    write_release(release, "2.0.0")
    assert invoke(release, target, "--upgrade").returncode == 0
    p = next((target / "AGENTVAULT_INSTALL_HISTORY").glob("*/before/scripts/runtime.py"))
    p.write_text("Corrupt backup\n")
    before = snapshot(target)
    result = invoke(target, target, "--rollback", "latest")
    assert result.returncode == 1 and "before-image hash mismatch" in result.stderr
    assert snapshot(target) == before


def test_concurrent_initial_installs_leave_one_consistent_managed_image(release, tmp_path):
    target = tmp_path / "project"
    command = [sys.executable, "-B", str(release / "scripts/coord/avcoord.py"), "init", "--target", str(target)]
    processes = [subprocess.Popen(command, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")) for _ in range(2)]
    results = [p.communicate(timeout=20) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_text())
    for rel, meta in receipt["managed_files"].items():
        assert sha((target / rel).read_bytes()) == meta["sha256"]


def interrupted(release, target, phase, *flags):
    """Kill a separate process after an actual durable write, without a production hook."""
    script = r'''
import importlib.util, os, sys
from pathlib import Path
source, target, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location("installer_crash_test", source)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
real = mod._install_write_bytes
def crash(dest, data, mode="0644"):
    real(dest, data, mode)
    pending = target / "AGENTVAULT_INSTALL_PENDING.json"
    matches = {
        "prepared": dest == pending,
        "code": dest == target / "scripts/runtime.py" and pending.exists(),
        "receipt": dest == target / "AGENTVAULT_INSTALL.json" and pending.exists(),
        "completion": dest.name == "result.json" and "transactions" in dest.parts and pending.exists(),
        "seed": dest == target / "MemoryBank/CURRENT.md" and pending.exists(),
        "recovery": dest == target / "scripts/zz_added.py" and pending.exists(),
    }
    if matches[phase]:
        os._exit(73)
mod._install_write_bytes = crash
raise SystemExit(mod.main(["init", "--target", str(target), *sys.argv[4:]]))
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("AVCOORD_ROOT", None)
    return subprocess.run([sys.executable, "-B", "-c", script, str(release / "scripts/coord/avcoord.py"),
                           str(target), phase, *flags], cwd=target.parent, capture_output=True, text=True, env=env)


def upgraded_source(release):
    (release / "scripts/runtime.py").write_text("durable version two\n")
    (release / "scripts/zz_added.py").write_text("version two added file\n")
    write_release(release, "2.0.0")


@pytest.mark.parametrize("phase", ["prepared", "code", "receipt"])
@pytest.mark.parametrize("direction", ["resume", "rollback"])
def test_killed_upgrade_recovers_from_each_durable_boundary(release, tmp_path, phase, direction):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    old = (target / "scripts/runtime.py").read_bytes()
    upgraded_source(release)
    killed = interrupted(release, target, phase, "--upgrade")
    assert killed.returncode == 73, killed.stdout + killed.stderr
    pending = target / "AGENTVAULT_INSTALL_PENDING.json"
    assert pending.exists()
    state = target / "MemoryBank/CURRENT.md"
    state.write_text("User work created after interruption\n")
    blocked = invoke(release, target, "--upgrade")
    assert blocked.returncode == 1 and "unfinished software transaction" in blocked.stderr
    before = snapshot(target)
    preview = invoke(release, target, "--recover", direction, "--preview")
    assert preview.returncode == 0, preview.stderr + preview.stdout
    assert snapshot(target) == before
    recovery = invoke(release, target, "--recover", direction)
    assert recovery.returncode == 0, recovery.stderr + recovery.stdout
    assert not pending.exists()
    expected = b"durable version two\n" if direction == "resume" else old
    assert (target / "scripts/runtime.py").read_bytes() == expected
    assert (target / "scripts/zz_added.py").exists() is (direction == "resume")
    assert state.read_text() == "User work created after interruption\n"
    receipt = json.loads((target / "AGENTVAULT_INSTALL.json").read_bytes())
    assert receipt["release_version"] == ("2.0.0" if direction == "resume" else "1.0.0")
    for rel, meta in receipt["managed_files"].items():
        assert sha((target / rel).read_bytes()) == meta["sha256"]
    after = snapshot(target)
    assert invoke(release, target, "--recover", direction).returncode == 0
    assert snapshot(target) == after


def test_recovery_can_itself_be_killed_and_resumed(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    upgraded_source(release)
    assert interrupted(release, target, "code", "--upgrade").returncode == 73
    assert interrupted(release, target, "recovery", "--recover", "resume").returncode == 73
    assert invoke(release, target, "--recover", "resume").returncode == 0
    assert not (target / "AGENTVAULT_INSTALL_PENDING.json").exists()
    assert (target / "scripts/zz_added.py").read_text() == "version two added file\n"


def test_completed_transaction_cleanup_cannot_reverse_a_committed_upgrade(release, tmp_path):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    upgraded_source(release)
    assert interrupted(release, target, "completion", "--upgrade").returncode == 73
    result = invoke(release, target, "--recover", "rollback")
    assert result.returncode == 0, result.stderr
    assert (target / "scripts/runtime.py").read_text() == "durable version two\n"
    assert not (target / "AGENTVAULT_INSTALL_PENDING.json").exists()


def test_interrupted_fresh_install_rollback_preserves_already_owned_seed(release, tmp_path):
    target = tmp_path / "project"
    assert interrupted(release, target, "seed").returncode == 73
    current = target / "MemoryBank/CURRENT.md"
    current.write_text("First user work\n")
    result = invoke(release, target, "--recover", "rollback")
    assert result.returncode == 0, result.stderr + result.stdout
    assert current.read_text() == "First user work\n"
    assert not (target / "AGENTVAULT_INSTALL.json").exists()
    assert not (target / "AGENTVAULT_INSTALL_PENDING.json").exists()
    assert invoke(release, target).returncode == 0
    assert current.read_text() == "First user work\n"


@pytest.mark.parametrize("corruption", ["code", "blob", "pending"])
def test_recovery_refuses_unrecorded_images_or_corrupt_evidence_before_writes(release, tmp_path, corruption):
    target = tmp_path / "project"
    assert invoke(release, target).returncode == 0
    upgraded_source(release)
    assert interrupted(release, target, "code", "--upgrade").returncode == 73
    pending = target / "AGENTVAULT_INSTALL_PENDING.json"
    intent = json.loads(pending.read_bytes())
    if corruption == "code":
        (target / "scripts/runtime.py").write_text("unrecorded local edit\n")
    elif corruption == "blob":
        content_sha = intent["files"]["scripts/zz_added.py"]["after"]["sha256"]
        (target / "AGENTVAULT_INSTALL_HISTORY/transactions" / intent["transaction_sha256"] / "blobs" / content_sha).write_bytes(b"corrupt")
    else:
        intent["operation"] = "tampered"
        pending.write_text(json.dumps(intent))
    before = snapshot(target)
    result = invoke(release, target, "--recover", "resume")
    assert result.returncode == 1
    assert snapshot(target) == before


def test_recovery_without_pending_intent_does_not_create_a_target(release, tmp_path):
    target = tmp_path / "absent"
    result = invoke(release, target, "--recover", "resume")
    assert result.returncode == 0 and not target.exists()
