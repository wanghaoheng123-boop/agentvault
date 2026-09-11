"""Process-contract remediation regressions — REVIEW-STRUCTURE-20260908 T01.

Covers behaviour added during the remediation that `test_process_contract.py` does not reach:
the sealed campaign's one permitted exit (a *final* execution against a frozen hash), the
separation of factor and baseline freeze lists, and trial lineage addressed by a stable
`trial_id` rather than by content hash.

Control: every refusal here is paired with the minimal variation that SUCCEEDS. A test that
only asserts a rejection cannot tell "the guard fired" from "the call was malformed"; the
paired success shows exactly which condition carries the refusal.
"""
from __future__ import annotations

import pytest

from aeap.engine import process_contract as pc

H = "a" * 64          # frozen factor hash
B = "b" * 64          # frozen baseline hash
UNFROZEN = "c" * 64   # never declared at close


def spec(**kw):
    base = dict(
        campaign_id="CAMP-REMEDIATION",
        decision_dates=("2021-06-30", "2021-12-31"),
        final_start="2022-06-30", final_end="2022-12-31",
        trial_budget=3, execution_budget=3, baseline_execution_budget=2,
        generator_budget=3, feedback_budget=1,
        policy_sha256=H, generator_sha256=H, corpus_sha256=H,
        initial_library_sha256=H, prompt_sha256=H, code_sha256=H,
    )
    base.update(kw)
    return pc.CampaignSpec(**base)


@pytest.fixture
def ledger(tmp_path):
    return pc.CampaignLedger(tmp_path / "campaign", spec())


def final_exec(ledger, kind="execution", factor=H):
    return ledger.record(kind, {"purpose": "final", "factor_sha256": factor})


def _open_final(ledger):
    """Open the holdout with evidence consistent with whatever the ledger has recorded."""
    trials = sum(e["kind"] == "trial" for e in ledger.events())
    return ledger.open_final({
        "window": {"start": "2022-06-30", "end": "2022-12-31"},
        "uncertainty": {"se": 0.1}, "attempt_count": trials, "process": {"k": 1},
        "baseline": {"k": 0}, "label_sha256": H,
    })


# ------------------------------------------------- the one permitted exit from a closed campaign

def test_a_closed_campaign_permits_a_final_execution_of_a_frozen_factor(ledger):
    """Closing must not make the preregistered final run impossible — that is its purpose."""
    ledger.close([H])
    event = final_exec(ledger)
    assert event["kind"] == "execution"
    assert event["body"]["purpose"] == "final"


def test_an_unfrozen_factor_cannot_ride_the_final_execution_exemption(ledger):
    """The exemption is for what was frozen, not for anything labelled `final`.

    Paired control: the identical call with the frozen hash succeeds above.
    """
    ledger.close([H])
    with pytest.raises(ValueError, match="final execution factor differs from closed campaign"):
        final_exec(ledger, factor=UNFROZEN)


def test_ordinary_work_is_still_refused_after_close(ledger):
    """Only `purpose == "final"` is exempt; the seal must not become a general reopening."""
    ledger.close([H])
    with pytest.raises(ValueError, match="campaign closed; no further tuning or feedback"):
        ledger.record("execution", {"factor_sha256": H})
    with pytest.raises(ValueError, match="campaign closed; no further tuning or feedback"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C"})


def test_a_final_execution_cannot_precede_the_close(ledger):
    """Running the final evaluation before freezing would defeat the freeze."""
    with pytest.raises(ValueError, match="close campaign before final execution"):
        final_exec(ledger)
    ledger.close([H])
    assert final_exec(ledger)["body"]["factor_sha256"] == H


def test_the_seal_outranks_the_final_execution_exemption(ledger):
    """Once final labels are opened nothing further may be recorded, final or not."""
    ledger.close([H])
    assert final_exec(ledger)["body"]["purpose"] == "final"   # permitted while merely closed
    _open_final(ledger)
    with pytest.raises(ValueError, match="permanently sealed"):
        final_exec(ledger)


# ------------------------------------------------------ factor and baseline freeze lists

def test_baseline_and_factor_freeze_lists_do_not_authorize_each_other(ledger):
    """A baseline is not a factor. Sharing one list would let an unfrozen factor through."""
    ledger.close([H], [B])

    assert final_exec(ledger, kind="baseline_execution", factor=B)["kind"] == "baseline_execution"
    assert final_exec(ledger, kind="execution", factor=H)["kind"] == "execution"

    with pytest.raises(ValueError, match="final execution factor differs from closed campaign"):
        final_exec(ledger, kind="baseline_execution", factor=H)
    with pytest.raises(ValueError, match="final execution factor differs from closed campaign"):
        final_exec(ledger, kind="execution", factor=B)


def test_closing_without_baselines_freezes_an_empty_baseline_list(ledger):
    """An omitted baseline list must mean "none permitted", never "any permitted"."""
    closed = ledger.close([H])
    assert closed["body"]["frozen_baseline_hashes"] == []
    with pytest.raises(ValueError, match="final execution factor differs from closed campaign"):
        final_exec(ledger, kind="baseline_execution", factor=H)


# ------------------------------------------------------------------ trial lineage by trial_id

def test_lineage_resolves_a_parent_by_its_declared_trial_id(ledger):
    """A stable id lets a child name its parent without depending on the parent's byte hash."""
    parent = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1",
                                     "trial_id": "TR-1"})
    child = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                    "lineage": "mutation", "parent_trial": "TR-1"})
    assert child["body"]["parent_trial"] == "TR-1"
    assert parent["body"]["trial_id"] == "TR-1"


def test_a_declared_trial_id_is_the_only_handle_for_that_parent(ledger):
    """Once a trial declares an id, its content hash is no longer an accepted reference.

    This pins a consequence of the lookup `body.get("trial_id", sha256)`: the declared id
    replaces the hash rather than supplementing it, so each trial has exactly one handle. The
    rationale is not recorded anywhere, so treat this as a description of current behaviour --
    if the exclusivity was incidental and both handles should resolve, change the lookup and
    this test together.

    Paired control: the same call naming `TR-1` succeeds in the test above.
    """
    parent = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1",
                                     "trial_id": "TR-1"})
    with pytest.raises(ValueError, match="not a retained earlier trial"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                "lineage": "mutation", "parent_trial": parent["sha256"]})


def test_an_unrecorded_trial_id_is_refused(ledger):
    ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1", "trial_id": "TR-1"})
    with pytest.raises(ValueError, match="not a retained earlier trial"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                "lineage": "mutation", "parent_trial": "TR-NEVER-RECORDED"})


# ------------------------------------------------------------------ readiness stays honest

def test_the_released_report_never_claims_isolation_or_provenance_it_has_not_earned(ledger):
    """Remediation added these flags; they must be present and False, not absent-and-assumed.

    An absent key reads as "not applicable" to a downstream consumer; an explicit False is a
    standing statement that the campaign has not earned the claim.
    """
    ledger.close([H])
    report = _open_final(ledger)
    for flag in ("performance_headline_eligible", "process_os_isolated",
                 "data_provenance_verified", "historical_implementability",
                 "net_implementability", "combined_portfolio_evaluated"):
        assert flag in report, f"{flag} must be stated, not omitted"
        assert report[flag] is False, f"{flag} must not default to a claim"
    assert report["readiness"] == "SYNTHETIC_MECHANICS_ONLY"
    assert report["production_admissions"] == 0
