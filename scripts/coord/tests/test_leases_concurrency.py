"""RFC T06/T07 — same-role concurrency, expiry, and race barriers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import SRC, claim, ns, start_run


# ------------------------------------------------------------------ release polarity (live bug)

def test_narrow_release_does_not_drop_a_broad_lease(av):
    """Releasing one file must not delete a lease covering the whole subtree.

    The old symmetric predicate meant `release --resource scripts/coord/avcoord.py`
    deleted a live `scripts/coord/**` lease.
    """
    assert claim(av, "code_generator", "scripts/coord/**", ttl="30m") == 0
    av.cmd_release(
        ns(agent="code_generator", resource=["scripts/coord/avcoord.py"], all=False, run=None, token=None)
    )
    live = av.read_leases()
    assert [l["resources"] for l in live] == [["scripts/coord/**"]]


def test_broad_release_does_drop_a_narrow_lease(av):
    assert claim(av, "code_generator", "scripts/coord/avcoord.py", ttl="30m") == 0
    av.cmd_release(
        ns(agent="code_generator", resource=["scripts/coord/**"], all=False, run=None, token=None)
    )
    assert av.read_leases() == []


# --------------------------------------------------------------------------- T06 same role

def test_two_runs_same_role_conflict_on_one_resource(av):
    a, b = start_run(av, "orchestrator"), start_run(av, "orchestrator")
    assert a != b
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=a) == 0
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=b) == 1


def test_two_runs_same_role_proceed_on_disjoint_resources(av):
    a, b = start_run(av, "orchestrator"), start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=a) == 0
    assert claim(av, "orchestrator", "reports/a.md", ttl="10m", run=b) == 0
    assert len(av.read_leases()) == 2


def test_authorization_preserves_case_on_case_sensitive_paths(av):
    """Claim exclusion may fold case, but a write capability must retain exact identity."""
    upper = av.canon_resource("reports/A.md")
    lower = av.canon_resource("reports/a.md")
    assert av.resources_overlap(upper, lower), "claims remain conservatively exclusive"
    assert not av.res_covers(upper, lower), "a lease must not widen across case-distinct paths"


def test_symlink_retarget_invalidates_captured_lease(av):
    first, second = av.ROOT / "reports" / "first", av.ROOT / "reports" / "second"
    first.mkdir()
    second.mkdir()
    link = av.ROOT / "reports" / "current"
    link.symlink_to(first, target_is_directory=True)
    assert claim(av, "doc_writer", "reports/current/**", ttl="30m") == 0
    lease = av.read_leases()[0]
    assert any(av.res_covers(item, av.canon_resource("reports/first/result.md"))
               for item in av.lease_resources(lease))

    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert av.lease_resources(lease) == []
    assert not any(av.res_covers(item, av.canon_resource("reports/second/result.md"))
                   for item in av.lease_resources(lease))


def test_legacy_agent_reclaim_stays_idempotent(av):
    """No run identity means agent-level semantics, exactly as before."""
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="20m") == 0


def test_run_cannot_renew_another_runs_lease(av):
    a, b = start_run(av, "orchestrator"), start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=a) == 0
    token = av.read_leases()[0]["lease_token"]
    assert av.cmd_renew(ns(agent="orchestrator", ttl="30m", run=b, token=token)) == 1


def test_unregistered_run_id_grants_nothing(av):
    """An arbitrary string is not an identity."""
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run="run-made-up") == 1


def test_run_belonging_to_another_agent_is_rejected(av):
    r = start_run(av, "code_generator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=r) == 1


def test_wrong_token_cannot_renew(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    before = av.read_leases()[0]["expires_at"]
    assert av.cmd_renew(ns(agent="orchestrator", ttl="60m", run=None, token="deadbeef")) == 1
    assert av.read_leases()[0]["expires_at"] == before


def test_correct_token_renews(av):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    lease = av.read_leases()[0]
    assert av.cmd_renew(ns(agent="orchestrator", ttl="60m", run=None, token=lease["lease_token"])) == 0
    assert av.read_leases()[0]["expires_at"] > lease["expires_at"]


def test_run_cannot_release_another_runs_lease(av):
    a, b = start_run(av, "orchestrator"), start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=a) == 0
    av.cmd_release(
        ns(agent="orchestrator", resource=["MemoryBank/CURRENT.md"], all=False, run=b, token=None)
    )
    assert len(av.read_leases()) == 1


# ------------------------------------------------------- release --all under a run (live bug)
#
# `--all` cannot carry a per-lease token: each lease has its own, so no single --token can
# ever match more than one. Demanding one anyway skipped every lease and swept nothing,
# leaving `release --resource X --token <that lease's token>`, one call per lease, as the
# only working path. An authenticated run is credential enough for --all; a targeted
# --resource release stays fail-closed and still requires --token.


def _release(av, agent, run=None, **kw):
    """cmd_release with the run secret attached — resolve_run() rejects a run without it.

    Omitting run_token makes release fail authentication before it reads a single lease
    file, so a test written without it passes on the error path and proves nothing about
    principal, fence or token handling.
    """
    kw.setdefault("resource", None)
    kw.setdefault("all", False)
    kw.setdefault("token", None)
    if run is not None:
        kw["run_token"] = av._test_tokens[run]
    return av.cmd_release(ns(agent=agent, run=run, **kw))


def test_release_all_under_a_run_drops_every_lease_that_run_holds(av, capsys):
    run = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", ["scripts/coord/**", "docs/**"], ttl="60m", run=run) == 0
    assert len(av.read_leases()) == 2

    assert _release(av, "orchestrator", run=run, all=True) == 0
    assert av.read_leases() == []

    # Sweeping an empty hand is distinguishable from having swept something, rather than
    # printing the same "released 0" the broken token gate did.
    capsys.readouterr()
    assert _release(av, "orchestrator", run=run, all=True) == 1
    assert "no lease is held" in capsys.readouterr().err


def test_release_all_is_bound_to_the_calling_run(av):
    """--all sweeps the caller's own leases, never a sibling run's."""
    a, b = start_run(av, "orchestrator"), start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "scripts/coord/**", ttl="60m", run=a) == 0
    assert claim(av, "orchestrator", "docs/**", ttl="60m", run=b) == 0

    assert _release(av, "orchestrator", run=a, all=True) == 0
    assert [l["resources"] for l in av.read_leases()] == [["docs/**"]]


def test_release_all_with_a_stale_fence_releases_nothing_and_says_why(av, capsys):
    """A lease from an older generation of this run is denied, not silently skipped.

    No CLI path produces this state today — only `run start` writes a run record, and it
    stamps `run_fence` == the run's fence at claim time — so the lease is aged on disk.
    That is the state the branch exists for: `lease.get("run_fence", lease.get("fence", 0))`
    falls back to the lease generation id precisely to judge leases written before the
    current `run_fence` field, i.e. ones already on disk from an earlier generation.
    """
    run = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "scripts/coord/**", ttl="60m", run=run) == 0
    lease_file = next(av.LEASES.glob("*.json"))
    stored = json.loads(lease_file.read_text())
    stored["run_fence"] = int(stored["run_fence"]) - 1  # aged out by a later generation
    lease_file.write_text(json.dumps(stored))
    capsys.readouterr()

    assert _release(av, "orchestrator", run=run, all=True) == 1
    captured = capsys.readouterr()
    assert "stale fence" in captured.err
    assert "released nothing" in captured.err, "a matchless release must not read as a no-op"
    assert len(av.read_leases()) == 1, "a stale fence must not release the lease"


def test_targeted_release_with_a_wrong_token_still_refuses(av, capsys):
    run = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "scripts/coord/**", ttl="60m", run=run) == 0
    capsys.readouterr()

    rc = _release(av, "orchestrator", run=run, resource=["scripts/coord/**"], token="not-the-token")
    assert rc == 1
    assert "token mismatch" in capsys.readouterr().err
    assert len(av.read_leases()) == 1


def test_targeted_release_with_the_right_token_succeeds(av):
    """Anchors the refusal above: the fail-closed path is still usable with a real token."""
    run = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "scripts/coord/**", ttl="60m", run=run) == 0
    held = av.read_leases()[0]

    rc = _release(av, "orchestrator", run=run, resource=["scripts/coord/**"], token=held["lease_token"])
    assert rc == 0
    assert av.read_leases() == []


# ------------------------------------------------------------------------ T07 expiry / races

def test_expired_lease_is_reclaimable_and_reads_do_not_reap(av, clock):
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    lease_files = list((av.LEASES).glob("*.json"))
    assert len(lease_files) == 1

    clock.advance(minutes=11)
    # A pure read must filter the expired lease out WITHOUT deleting the file.
    assert av.read_leases() == []
    assert lease_files[0].exists(), "read_leases() must not reap"

    # Health commands are read-only too.
    av.cmd_status(ns(json=False))
    assert lease_files[0].exists(), "cmd_status must not reap"

    # Only reap (and claim) remove it.
    av.cmd_reap(ns(agent="orchestrator"))
    assert not lease_files[0].exists()


def test_reclaim_after_expiry_then_stale_writer_denied(av, clock):
    a = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=a) == 0
    old_token = av.read_leases()[0]["lease_token"]
    clock.advance(minutes=11)
    b = start_run(av, "code_generator")  # a later run gets a strictly higher fence
    assert claim(av, "code_generator", "MemoryBank/CURRENT.md", ttl="10m", run=b) == 0
    # The paused original writer must not be able to renew what it no longer holds.
    assert av.cmd_renew(ns(agent="orchestrator", ttl="30m", run=a, token=old_token)) == 1


def test_fence_rejects_a_stale_run_acting_on_a_reclaimed_resource(av, clock):
    """The decisive fencing case: an older generation cannot release a newer lease."""
    old = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "reports/x.md", ttl="10m", run=old) == 0
    clock.advance(minutes=11)
    new = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "reports/x.md", ttl="10m", run=new) == 0
    held = av.read_leases()[0]
    assert held["fence"] > 0
    av.cmd_release(ns(agent="orchestrator", resource=["reports/x.md"], all=False, run=old, token=None))
    assert len(av.read_leases()) == 1, "a stale fence must not release the newer lease"


def test_fences_are_strictly_monotonic(av):
    fences = []
    for _ in range(3):
        rid = start_run(av, "orchestrator")
        fences.append(json.loads((av.COORD / "runs" / f"{rid}.json").read_text())["fence"])
    assert fences == sorted(fences) and len(set(fences)) == 3


def test_legacy_callers_keep_working_without_a_run(av):
    """No --run means fence 0 and agent-level semantics: the wrappers must not break."""
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m") == 0
    assert av.cmd_renew(ns(agent="orchestrator", ttl="30m", run=None, token=None)) == 0


def test_concurrent_claims_leave_exactly_one_winner(av):
    script = textwrap.dedent(
        f"""
        import importlib.util, sys, json
        spec = importlib.util.spec_from_file_location("av", {str(SRC / 'avcoord.py')!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        sys.exit(m.main(["claim", "--agent", sys.argv[1], "--resource", "MemoryBank/CURRENT.md",
                         "--ttl", "10m", "--reason", "race"]))
        """
    )
    env = dict(os.environ, AVCOORD_ROOT=str(av.ROOT))
    procs = [
        subprocess.Popen([sys.executable, "-c", script, agent],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for agent in ("orchestrator", "code_generator", "doc_writer")
    ]
    codes = [p.wait() for p in procs]
    assert codes.count(0) == 1, f"expected exactly one winner, got {codes}"
    assert len(list(av.LEASES.glob("*.json"))) == 1


def _barrier_process(av, barrier: Path, argv: list[str], *, env: dict) -> subprocess.Popen:
    script = textwrap.dedent(
        f"""
        import importlib.util, json, sys, time
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("av_race", {str(SRC / 'avcoord.py')!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        deadline = time.monotonic() + 5
        while not Path(sys.argv[1]).exists():
            if time.monotonic() > deadline: raise SystemExit(99)
            time.sleep(0.005)
        raise SystemExit(m.main(json.loads(sys.argv[2])))
        """
    )
    return subprocess.Popen([sys.executable, "-c", script, str(barrier), json.dumps(argv)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_synchronized_renew_release_race_cannot_resurrect_a_released_lease(av, tmp_path):
    run = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=run) == 0
    lease = av.read_leases()[0]
    barrier = tmp_path / "renew-release.go"
    env = dict(os.environ, AVCOORD_ROOT=str(av.ROOT), AVCOORD_RUN_TOKEN=av._test_tokens[run])
    renew = _barrier_process(av, barrier, ["renew", "--agent", "orchestrator", "--ttl", "30m",
                                           "--run", run, "--token", lease["lease_token"]], env=env)
    release = _barrier_process(av, barrier, ["release", "--agent", "orchestrator",
                                             "--resource", "MemoryBank/CURRENT.md", "--run", run,
                                             "--token", lease["lease_token"]], env=env)
    barrier.touch()
    codes = [renew.wait(timeout=10), release.wait(timeout=10)]
    assert codes[1] == 0 and codes[0] in {0, 1}
    assert av.read_leases() == []


def test_synchronized_reap_reclaim_race_preserves_the_new_generation(av, clock, tmp_path):
    old = start_run(av, "orchestrator")
    assert claim(av, "orchestrator", "MemoryBank/CURRENT.md", ttl="10m", run=old) == 0
    clock.advance(minutes=11)
    new = start_run(av, "code_generator")
    run_path = av.COORD / "runs" / f"{new}.json"
    run_record = json.loads(run_path.read_text())
    run_record["expires_at"] = "2099-01-01T00:00:00+00:00"
    run_path.write_text(json.dumps(run_record))
    barrier = tmp_path / "reap-reclaim.go"
    env = dict(os.environ, AVCOORD_ROOT=str(av.ROOT), AVCOORD_RUN_TOKEN=av._test_tokens[new])
    reclaim = _barrier_process(av, barrier, ["claim", "--agent", "code_generator",
                                             "--resource", "MemoryBank/CURRENT.md", "--ttl", "10m",
                                             "--run", new], env=env)
    reap = _barrier_process(av, barrier, ["reap", "--agent", "orchestrator"], env=env)
    barrier.touch()
    assert [reclaim.wait(timeout=10), reap.wait(timeout=10)] == [0, 0]
    live = av.read_leases()
    assert len(live) == 1 and live[0]["run_id"] == new and live[0]["agent_id"] == "code_generator"


def test_a_live_lock_is_never_stolen(av):
    """The old lock was handed to a waiter purely because it was 30s old.

    A holder that keeps the lock past that threshold must still be respected: the second
    process blocks and then succeeds, and the first holder's work is intact.
    """
    lock_path = av.LEASES / ".claim.lock"
    av.LEASES.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, time
            fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(2.0)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        with av.FileLock(lock_path, timeout=0.5):
            pass
    assert time.monotonic() - t0 >= 0.4, "should have blocked, not stolen"
    holder.wait()
    # Once the holder exits the kernel releases it and the lock is acquirable again.
    with av.FileLock(lock_path, timeout=2.0):
        pass


def test_lock_released_when_holder_dies(av):
    lock_path = av.LEASES / ".claim.lock"
    av.LEASES.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, os, time
            fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("held", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    assert p.stdout.readline().strip() == "held"
    p.kill()
    p.wait()
    with av.FileLock(lock_path, timeout=2.0):
        pass  # no stale threshold needed; the kernel already released it
