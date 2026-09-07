# AEAP — automated empirical alpha pipeline

Typed proposals, versioned policies, an AST firewall, contained execution, and nine
independent gates. Implements RFC-WORKSPACE-AEAP-20260906 sections 6 and 8.

## Admission is disabled

Not a configuration accident — a stated activation blocker:

- **Containment is not isolation.** Execution runs in a subprocess with rlimits and a
  scrubbed environment. That stops runaway loops and accidents. It does **not** contain a
  hostile generator sharing the same uid, and AST filtering is not a security boundary
  either. `aeap/policies/sandbox.v1.yaml` records `is_security_boundary: false`.
- **No point-in-time data exists here.** Thresholds are `CALIBRATED_SYNTHETIC`, derived
  from the null distribution of our own estimator on synthetic panels. They cannot stand in
  for real market thresholds.
- **Model vintage is unresolved.** A modern model may carry knowledge from after a replay's
  decision date even when files and data are filtered. Every report carries
  `MODEL_VINTAGE_CONTAMINATION_UNRESOLVED`.

A candidate can therefore reach `all_gates_pass: true` and still be `admissible: false`.
That is the intended behavior.

## Layout

| Path | Contents |
|------|----------|
| [`contracts/`](contracts/) | JSON schemas every artifact must satisfy |
| [`policies/`](policies/) | Frozen, versioned policy files |
| [`campaigns/`](campaigns/) | Preregistrations and the calibration receipt |
| [`datasets/`](datasets/) | Snapshot manifests. The yfinance adapter is `pit_compliant: false` |
| [`engine/`](engine/) | Firewall, sandbox, operators, nine gates, evaluation |
| [`engine/gates/`](engine/gates/) | One module per gate |
| [`tests/`](tests/) | Firewall, gate, sandbox and PIT contract tests |

## Engine modules

| Module | Role |
|--------|------|
| [`engine/firewall.py`](engine/firewall.py) | AST validation before any data handle exists |
| [`engine/sandbox.py`](engine/sandbox.py) | Contained execution; reports which rlimits actually applied |
| [`engine/operators.py`](engine/operators.py) | The typed operator vocabulary; trailing windows only |
| [`engine/evaluate.py`](engine/evaluate.py) | Orchestration and the gate report |
| [`engine/calibrate.py`](engine/calibrate.py) | Derives and freezes thresholds from null panels |
| [`engine/panels.py`](engine/panels.py) | Deterministic synthetic panels |
| [`engine/reference.py`](engine/reference.py) | Frozen as-of reference set for G1/G6/G7 |
| [`engine/adapters/yfinance_dev.py`](engine/adapters/yfinance_dev.py) | DEV-ONLY, `pit_compliant: false` |

## Order of operations

    firewall (no data)  ->  sandbox (features only, no labels)  ->  gates (labels)

The judge is the first component that touches labels. The generator and the executor never
receive them.

## Run it

```bash
python3 -m aeap.engine.calibrate --replications 300 --freeze   # derive + freeze thresholds
python3 -m pytest aeap/tests/ -q                               # contract tests
```

Entry: [`../WORKSPACE_INDEX.md`](../WORKSPACE_INDEX.md)
