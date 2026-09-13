"""RFC T19 — search accounting and the sealed final holdout.

This ledger is the mechanism that stops adaptive overfitting and final-test leakage, so
its refusals matter more than its successes. Each test below asserts a REFUSAL that the
campaign depends on: a budget that cannot be exceeded, a clock that cannot run backwards,
a seal that cannot be reopened, and evidence that cannot be edited after the fact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from aeap.engine import process_contract as pc

H = "a" * 64


def spec(**kw):
    base = dict(
        campaign_id="CAMP-TEST",
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


# ------------------------------------------------------------------ preregistration

def test_final_window_must_follow_every_decision_date():
    """A holdout overlapping discovery is not a holdout."""
    with pytest.raises(ValueError, match="final holdout must follow"):
        spec(final_start="2021-01-01")


def test_decision_dates_must_be_unique_and_increasing():
    with pytest.raises(ValueError, match="unique and strictly increasing"):
        spec(decision_dates=("2021-12-31", "2021-06-30"))
    with pytest.raises(ValueError, match="unique and strictly increasing"):
        spec(decision_dates=("2021-06-30", "2021-06-30"))


def test_every_frozen_input_requires_a_real_hash():
    for field in ("policy_sha256", "generator_sha256", "corpus_sha256",
                  "initial_library_sha256", "prompt_sha256", "code_sha256"):
        with pytest.raises(ValueError, match="frozen SHA-256"):
            spec(**{field: "not-a-hash"})


def test_budgets_must_be_nonnegative_ints():
    with pytest.raises(ValueError, match="nonnegative integer"):
        spec(trial_budget=-1)
    with pytest.raises(ValueError, match="nonnegative integer"):
        spec(trial_budget=1.5)


def test_evidence_store_may_not_live_inside_the_workspace(tmp_path):
    """Replay evidence is a fixture, not production state."""
    workspace = Path(pc.__file__).resolve().parents[2]
    with pytest.raises(Exception):
        pc.CampaignLedger(workspace / "aeap" / "campaigns" / "leak", spec())


# ----------------------------------------------------------------- budget accounting

def test_trial_budget_cannot_be_exceeded(ledger):
    for i in range(3):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": f"C{i}"})
    with pytest.raises(ValueError, match="trial budget exhausted"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C3"})


def test_each_budget_is_metered_separately(ledger):
    for i in range(3):
        ledger.record("generation", {"decision_date": "2021-06-30", "n": i})
    with pytest.raises(ValueError, match="generation budget exhausted"):
        ledger.record("generation", {"decision_date": "2021-06-30", "n": 99})
    # A separate budget is unaffected by the exhausted one.
    ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C"})


def test_feedback_exposure_is_metered(ledger):
    ledger.record("feedback", {"decision_date": "2021-06-30", "fields": ["gate_status"]})
    with pytest.raises(ValueError, match="feedback budget exhausted"):
        ledger.record("feedback", {"decision_date": "2021-06-30", "fields": ["gate_status"]})


# ------------------------------------------------------------------ temporal ordering

def test_decision_dates_cannot_move_backwards(ledger):
    ledger.record("trial", {"decision_date": "2021-12-31", "candidate": "C1"})
    with pytest.raises(ValueError, match="moved backwards"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2"})


def test_decision_date_outside_the_frozen_campaign_is_refused(ledger):
    with pytest.raises(ValueError, match="outside frozen campaign"):
        ledger.record("trial", {"decision_date": "2023-01-01", "candidate": "C"})


# ----------------------------------------------------------------------- lineage

def test_a_repair_must_name_a_retained_parent(ledger):
    with pytest.raises(ValueError, match="require parent trial"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                "lineage": "repair"})


def test_a_parent_must_actually_be_an_earlier_trial(ledger):
    with pytest.raises(ValueError, match="not a retained earlier trial"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                "lineage": "mutation", "parent_trial": "f" * 64})


def test_a_real_parent_is_accepted(ledger):
    parent = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1"})
    child = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2",
                                    "lineage": "mutation", "parent_trial": parent["sha256"]})
    assert child["body"]["parent_trial"] == parent["sha256"]


def test_unknown_lineage_is_refused(ledger):
    with pytest.raises(ValueError, match="unknown trial lineage"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C",
                                "lineage": "creative_reinterpretation"})


# --------------------------------------------------------------- the sealed holdout

def test_final_labels_cannot_be_opened_before_the_campaign_closes(ledger):
    with pytest.raises(ValueError, match="close campaign before opening"):
        ledger.record("final_opened", {})


def test_opening_the_final_test_seals_the_campaign_permanently(ledger):
    ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1"})
    ledger.close([H])
    ledger.open_final({
        "window": {"start": "2022-06-30", "end": "2022-12-31"},
        "uncertainty": {"se": 0.01}, "attempt_count": 1,
        "process": {"admits": 0}, "baseline": {"admits": 0}, "label_sha256": H,
    })
    # Nothing may follow — not another trial, not a lesson, not a second opening.
    for kind in ("trial", "lesson", "feedback", "final_opened"):
        with pytest.raises(ValueError, match="permanently sealed"):
            ledger.record(kind, {"decision_date": "2021-06-30"})


def test_closed_campaign_refuses_further_tuning(ledger):
    ledger.close([H])
    with pytest.raises(ValueError, match="campaign closed"):
        ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C"})


def test_final_evidence_must_be_complete(ledger):
    ledger.close([H])
    with pytest.raises(ValueError, match="incomplete released final-process evidence"):
        ledger.open_final({"window": {"start": "2022-06-30", "end": "2022-12-31"}})


def test_final_evidence_window_must_match_preregistration(ledger):
    ledger.close([H])
    with pytest.raises(ValueError, match="window differs from preregistration"):
        ledger.open_final({
            "window": {"start": "2022-01-01", "end": "2022-12-31"},
            "uncertainty": {}, "attempt_count": 1, "process": {}, "baseline": {},
            "label_sha256": H,
        })


# ------------------------------------------------------------------ evidence integrity

def test_events_are_hash_linked(ledger):
    a = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1"})
    b = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C2"})
    assert a["prev_sha256"] == "0" * 64
    assert b["prev_sha256"] == a["sha256"]
    assert b["seq"] == a["seq"] + 1


def test_events_are_write_once_through_the_api(ledger, tmp_path):
    """`immutable_json` is create-exclusive: rewriting a path with different bytes raises,
    while rewriting identical bytes is idempotent.

    Note the scope, which the module states about itself: this is write-once through the
    API, NOT an OS trust boundary. Files stay mode 0644 and any process with filesystem
    access can still edit them. Detection of that lives in the hash chain and in
    read_scores, not in permissions.
    """
    ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1"})
    files = list((ledger.root / "events").glob("*.json"))
    assert files

    p = tmp_path / "artifact.json"
    pc.immutable_json(p, {"a": 1})
    pc.immutable_json(p, {"a": 1})                      # identical bytes: no-op
    with pytest.raises(ValueError, match="different bytes"):
        pc.immutable_json(p, {"a": 2})


def test_tampering_with_an_event_file_breaks_the_hash_chain(ledger):
    """Since files are not OS-protected, integrity has to be detectable after the fact."""
    a = ledger.record("trial", {"decision_date": "2021-06-30", "candidate": "C1"})
    path = next((ledger.root / "events").glob("*.json"))
    payload = json.loads(path.read_bytes())
    payload["body"]["candidate"] = "TAMPERED"
    path.write_text(json.dumps(payload))
    assert pc.digest({k: v for k, v in payload.items() if k != "sha256"}) != a["sha256"]


def test_score_artifacts_round_trip_under_their_hash(ledger):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2021-01-01", periods=3, freq="B"), ["A", "B"]], names=["date", "asset"])
    s = pd.Series(range(6), index=idx, dtype="float64")
    ref = ledger.scores(s)
    back = ledger.read_scores(ref)
    assert back.equals(s)


def test_an_empty_score_artifact_is_refused(ledger):
    idx = pd.MultiIndex.from_arrays([[], []], names=["date", "asset"])
    with pytest.raises(ValueError, match="empty or all-missing"):
        ledger.scores(pd.Series(dtype="float64", index=idx))


def test_tampered_score_artifact_is_detected(ledger):
    idx = pd.MultiIndex.from_product(
        [pd.date_range("2021-01-01", periods=2, freq="B"), ["A"]], names=["date", "asset"])
    ref = ledger.scores(pd.Series([1.0, 2.0], index=idx))
    path = ledger.root / ref["path"]
    path.chmod(0o644)
    payload = json.loads(path.read_bytes())
    payload["rows"][0][-1] = 99.0
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="content changed"):
        ledger.read_scores(ref)


def test_score_reference_cannot_escape_the_evidence_store(ledger):
    with pytest.raises(ValueError, match="escapes evidence store"):
        ledger.read_scores({"path": "../../../etc/passwd", "sha256": H, "rows": 1})


def test_unsupported_event_kind_is_refused(ledger):
    with pytest.raises(ValueError, match="unsupported process event"):
        ledger.record("please_just_admit_it", {})
