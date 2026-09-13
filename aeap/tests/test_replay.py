"""RFC T18/T19 — historical replay causality, budget accounting, and the production bar."""
from __future__ import annotations
import pathlib
import pytest
from aeap.engine import replay
from aeap.tests._policy import requires_calibration

pytestmark = requires_calibration

CANDS = [
    {"id": "C1", "expression": "cs_rank(ts_mean(x, 3))", "expected_sign": 1, "true_signal": 0.35},
    {"id": "C2", "expression": "cs_zscore(ts_mean(x, 5))", "expected_sign": -1, "true_signal": 0.35},
]


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    root = tmp_path_factory.mktemp("replay")
    return replay.run_replay(decision_dates=["2021-06-30", "2021-12-31"],
                             candidates=CANDS, fixture_root=root, n_dates=200,
                             candidate_budget=4)


@pytest.fixture(scope="module")
def perturbed_result(tmp_path_factory):
    """The same replay, rerun from an empty fixture with only FUTURE context added.

    The corpus entry below becomes known after every decision date, so an implementation that
    respects chronology must produce byte-identical earlier decisions. Reusing the first run's
    fixture root would let state carry over and hide exactly the leak this is testing for.
    """
    root = tmp_path_factory.mktemp("replay-perturbed")
    future_only = [{"id": "FUTURE-NOTE", "text": "context published after discovery ended",
                    "known_at": "2022-06-30", "valid_from": "2022-06-30"}]
    return replay.run_replay(decision_dates=["2021-06-30", "2021-12-31"],
                             candidates=CANDS, fixture_root=root, n_dates=200,
                             candidate_budget=4, corpus=future_only)


def test_replay_cannot_promote_production_membership(result):
    assert result["is_production"] is False
    assert result["can_promote_production_membership"] is False
    assert result["production_admissions"] == 0


def test_walk_forward_is_causal(result, perturbed_result):
    """A later admission must never become visible to an earlier decision date.

    Valid chronology is necessary but not sufficient, so the single-argument check reports
    INCONCLUSIVE by design — a monotone ordering of dates proves nothing about what the
    decisions actually consumed. Earning PASS requires an independent rerun in which only
    future-known context changed and every earlier decision fingerprint stayed identical.
    """
    chronology = replay.causality_check(result)
    assert chronology["status"] == "INCONCLUSIVE", chronology
    assert "rerun required" in chronology["reason"]

    c = replay.causality_check(result, perturbed_result)
    assert c["status"] == "PASS", c
    assert c["compared_decisions"] == 2, "both discovery dates must actually be compared"


def test_causality_fails_when_an_earlier_decision_moves(result, perturbed_result):
    """Control: the comparison must be able to fail, not just report PASS.

    Corrupting one earlier fingerprint in a copy of the perturbed run has to flip the verdict;
    otherwise the test above would pass even if fingerprints were never compared.
    """
    import copy as _copy
    tampered = _copy.deepcopy(perturbed_result)
    tampered["per_date"][0]["decision_fingerprint"] = "0" * 64
    assert replay.causality_check(result, tampered)["status"] == "FAIL"


def test_budget_is_metered_and_exhausts(result):
    assert result["budget_remaining"] == 0
    assert len(result["trials"]) == result["candidate_budget"]


def test_failed_trials_are_retained(result):
    """feedback.v1 forbids deleting failed or rejected trials."""
    assert any(t["all_gates_pass"] is False for t in result["trials"])


def test_wrong_preregistered_sign_never_passes(result):
    for d in result["per_date"]:
        for r in d["results"]:
            if r.get("candidate") == "C2" and "checks" in r:
                assert r["checks"]["G9_sign_alignment"] == "FAIL"


def test_every_result_carries_the_vintage_and_data_caveats(result):
    assert result["model_vintage_status"] == "MODEL_VINTAGE_CONTAMINATION_UNRESOLVED"
    assert result["data_status"] == "SYNTHETIC_NOT_PIT"


def test_replay_writes_only_inside_its_fixture_root(result):
    """Replay must never deposit a factor object in the production library.

    The previous form was `assert ... if prod_lib.exists() else True`, which short-circuited
    to True whenever the directory was absent — and it always was, because git does not
    track empty directories. The test passed by checking nothing. Adding a .gitkeep to the
    scaffold made the directory real and exposed that.

    Now it asserts the substance in both cases: no factor object, scaffold placeholder or
    not, and zero production admissions on the result itself.
    """
    prod_lib = pathlib.Path("aeap/library/objects")
    leaked = ([p for p in prod_lib.glob("*") if p.name != ".gitkeep"]
              if prod_lib.exists() else [])
    assert leaked == [], f"replay leaked factor objects into the production library: {leaked}"
    assert result["production_admissions"] == 0
    assert result["can_promote_production_membership"] is False
