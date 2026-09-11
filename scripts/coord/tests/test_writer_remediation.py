"""Writer authorization regressions — REVIEW-STRUCTURE-20260908 T01.

The review's finding: "A role label or lease is not access isolation." Appends now require a
registered, live run *and* a matching secret *and* lease proofs pinned to that run's fence, so
a stale or borrowed identity cannot write.

Every refusal below asserts the specific message, not merely that something failed — a test
that accepts any exception passes for the wrong reason as soon as setup drifts. The final
negative control neutralises the guard and shows the same call then succeeds, which is what
proves these assertions are load-bearing.
"""
from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from conftest import SRC, claim, expected_head, start_run

AUTH_REFUSED = "registered run and valid run token required"
LEASE_REFUSED = "missing live lease token/fence"


@pytest.fixture
def raw_cw(av):
    """commit_worker with no credential wrapper — these tests drive authorize() directly."""
    spec = importlib.util.spec_from_file_location("cw_writer", SRC / "commit_worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cw_writer"] = mod
    spec.loader.exec_module(mod)
    mod.av.configure_paths(av.ROOT)
    return mod


def _append(cw, task_id="T", status="open", **kw):
    return cw.commit("task.transition", {"task_id": task_id, "status": status}, **kw)


# ------------------------------------------------------------------- run identity and secret

def test_an_unregistered_run_cannot_append(raw_cw, monkeypatch):
    monkeypatch.setenv("AVCOORD_RUN_ID", "run-20260101T000000-deadbe")
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", "any-token")
    with pytest.raises(PermissionError, match=AUTH_REFUSED):
        _append(raw_cw)


def test_a_registered_run_with_the_wrong_secret_cannot_append(raw_cw, av, monkeypatch):
    run = start_run(av, "orchestrator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", av._test_tokens[run][::-1])
    with pytest.raises(PermissionError, match=AUTH_REFUSED):
        _append(raw_cw)


def test_an_empty_secret_is_not_treated_as_absent_verification(raw_cw, av, monkeypatch):
    """A blank token must fail closed rather than skip the comparison."""
    run = start_run(av, "orchestrator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", "")
    with pytest.raises(PermissionError, match=AUTH_REFUSED):
        _append(raw_cw)


def test_another_agents_run_cannot_be_borrowed(raw_cw, av, monkeypatch):
    """Credentials bind to the run's own agent; the caller cannot claim to be someone else."""
    run = start_run(av, "code_generator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", av._test_tokens[run])
    with pytest.raises(PermissionError, match=AUTH_REFUSED):
        _append(raw_cw, agent_id="orchestrator")


# --------------------------------------------------------------------------- lease proofs

def test_valid_credentials_still_require_a_live_lease(raw_cw, av, monkeypatch):
    run = start_run(av, "orchestrator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", av._test_tokens[run])
    monkeypatch.setenv("AVCOORD_LEASE_PROOFS_JSON", "[]")
    with pytest.raises(PermissionError, match=LEASE_REFUSED):
        _append(raw_cw)


def test_a_superseded_lease_proof_is_refused(raw_cw, av, monkeypatch):
    """A proof captured before the lease was re-taken must not authorize a later write.

    This is the fencing property: an agent that paused, lost its lease and had it reissued
    cannot resume with the generation it remembers.
    """
    run = start_run(av, "orchestrator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", av._test_tokens[run])
    assert claim(av, "orchestrator", ["MemoryBank/**"], run=run) == 0
    stale_proofs = av.read_leases()

    # Re-take the same resource; the lease is reissued with a new generation.
    assert claim(av, "orchestrator", ["MemoryBank/**"], run=run) == 0
    fresh_proofs = av.read_leases()
    assert [p["fence"] for p in fresh_proofs] != [p["fence"] for p in stale_proofs], \
        "re-claiming must issue a new fence, otherwise this test proves nothing"

    monkeypatch.setenv("AVCOORD_LEASE_PROOFS_JSON", json.dumps(stale_proofs))
    with pytest.raises(PermissionError, match=LEASE_REFUSED):
        _append(raw_cw)

    monkeypatch.setenv("AVCOORD_LEASE_PROOFS_JSON", json.dumps(fresh_proofs))
    assert _append(raw_cw, expected=expected_head(raw_cw, "task.transition",
                                                  {"task_id": "T"}))["status"] == "committed"


def test_a_lease_that_does_not_cover_the_target_is_refused(raw_cw, av, monkeypatch):
    """A lease on an unrelated subtree must not authorize a journal append."""
    run = start_run(av, "orchestrator")
    monkeypatch.setenv("AVCOORD_RUN_ID", run)
    monkeypatch.setenv("AVCOORD_RUN_TOKEN", av._test_tokens[run])
    assert claim(av, "orchestrator", ["scripts/**"], run=run) == 0
    monkeypatch.setenv("AVCOORD_LEASE_PROOFS_JSON", json.dumps(av.read_leases()))
    with pytest.raises(PermissionError, match=LEASE_REFUSED):
        _append(raw_cw)


# ------------------------------------------------------------------- happy path + control

def test_full_credentials_and_a_covering_lease_permit_the_append(raw_cw, writer_credentials):
    receipt = _append(raw_cw, expected=expected_head(raw_cw, "task.transition", {"task_id": "T"}))
    assert receipt["status"] == "committed"
    assert receipt["seq"] == 1
    assert len(raw_cw.read_prefix()) == 1


def test_the_credential_check_is_what_refuses_these_appends(raw_cw, monkeypatch):
    """Negative control for every refusal above.

    With authorize() neutralised, the identical unauthenticated call succeeds. That is what
    shows the PermissionErrors come from the credential guard rather than from missing
    directories, an unclean journal, or some other incidental setup gap.
    """
    monkeypatch.setattr(raw_cw, "authorize", lambda resources, agent_id, *a, **k: "run-stub")
    receipt = _append(raw_cw, expected=expected_head(raw_cw, "task.transition", {"task_id": "T"}))
    assert receipt["status"] == "committed", "the guard, not the fixture, must be the blocker"
