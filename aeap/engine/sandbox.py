"""B3 execution — run firewall-approved code with features only, never labels.

HONEST SCOPE, stated here because it is the reason admission is disabled:
this is a subprocess with POSIX rlimits, a scrubbed environment and no label columns in
the payload. That contains accidents, runaway loops and memory exhaustion. It is NOT an
OS security boundary against a hostile process running under the same uid. A real boundary
needs a separate principal whose capabilities the generating agent cannot reach.

`run-manifest.schema.json` therefore records `is_security_boundary: false` and names the
mechanism actually used, so no downstream reader can mistake containment for isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

TZ = timezone(timedelta(hours=8))
POLICY_PATH = Path(__file__).resolve().parents[1] / "policies" / "sandbox.v1.yaml"
ENGINE_ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import json, os, pickle, resource, sys
    from pathlib import Path

    limits = json.loads(sys.argv[1])
    feat_path, out_path, expr_path, libpath = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

    # Apply best-effort and report what actually took. Several rlimits are not settable
    # on every platform (RLIMIT_NPROC is per-user on macOS, RLIMIT_AS is often ignored), and
    # silently pretending an unenforced limit is in place would overstate containment.
    applied, skipped = {}, {}
    for name, value in (("RLIMIT_CPU", limits["cpu_seconds"]),
                        ("RLIMIT_AS", limits["memory_bytes"]),
                        ("RLIMIT_FSIZE", 256 * 1024 * 1024)):
        rid = getattr(resource, name, None)
        if rid is None:
            skipped[name] = "unavailable on this platform"
            continue
        try:
            soft, hard = resource.getrlimit(rid)
            target = value if hard in (resource.RLIM_INFINITY, -1) else min(value, hard)
            resource.setrlimit(rid, (target, hard))
            applied[name] = target
        except (ValueError, OSError) as e:
            skipped[name] = str(e)

    # -I gives an isolated interpreter (no env vars, no user site). Library paths are
    # therefore passed EXPLICITLY rather than inherited, so what the child can import is an
    # auditable list rather than whatever the parent's environment happened to contain.
    for entry in json.loads(libpath):
        if entry not in sys.path:
            sys.path.append(entry)
    sys.path.insert(0, os.environ["AEAP_ROOT"])
    from aeap.engine.operators import REGISTRY

    with open(feat_path, "rb") as f:
        features = pickle.load(f)
    expression = Path(expr_path).read_text()

    # The evaluation namespace contains ONLY the declared features and the allowlisted
    # operators. No builtins: __import__, open and getattr are simply absent.
    ns = {"__builtins__": {}}
    ns.update(REGISTRY)
    ns.update(features)
    result = eval(compile(expression, "<candidate>", "eval"), ns, {})

    with open(out_path, "wb") as f:
        pickle.dump(result, f, protocol=4)
    print(json.dumps({"rows": int(len(result)), "limits_applied": applied,
                      "limits_skipped": skipped}))
    """
).strip()


def load_policy(path: Path | None = None) -> tuple[dict, str]:
    p = path or POLICY_PATH
    raw = p.read_bytes()
    return yaml.safe_load(raw), hashlib.sha256(raw).hexdigest()


def _sha_obj(obj) -> str:
    return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()


def execute(expression: str, features: dict[str, pd.Series], *, candidate_id: str,
            executor_run_id: str, firewall_passed: bool,
            policy_path: Path | None = None) -> tuple[pd.Series | None, dict]:
    """Execute a candidate. Returns (scores, run_manifest).

    Refuses outright unless the firewall already passed: execution must never be the thing
    that discovers a candidate is malformed.
    """
    policy, policy_sha = load_policy(policy_path)
    started = datetime.now(TZ).isoformat(timespec="seconds")

    base_manifest = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "executor_run_id": executor_run_id,
        "started_at": started,
        "finished_at": started,
        "code_sha256": hashlib.sha256(expression.encode("utf-8")).hexdigest(),
        "feature_snapshot_sha256": _sha_obj({k: v for k, v in sorted(features.items())}),
        "output_sha256": hashlib.sha256(b"").hexdigest(),
        "row_count": 0,
        "status": "refused",
        "isolation": {
            "network": False,
            "labels_visible": False,
            "writable_paths": [],
            "cpu_seconds": float(policy["limits"]["cpu_seconds"]),
            "memory_bytes": int(policy["limits"]["memory_bytes"]),
            "enforced_by": policy["enforced_by"],
            "is_security_boundary": bool(policy["is_security_boundary"]),
        },
        "logs_path": None,
        "policy_sha256": policy_sha,
    }

    if not firewall_passed:
        base_manifest["status"] = "refused"
        base_manifest["refusal_reason"] = "firewall did not pass; execution is not attempted"
        return None, base_manifest

    lim = policy["limits"]
    with tempfile.TemporaryDirectory(prefix="aeap_sbx_") as tmp:
        tmp = Path(tmp)
        feat_p, out_p, expr_p = tmp / "features.pkl", tmp / "scores.pkl", tmp / "expr.txt"
        with feat_p.open("wb") as f:
            pickle.dump(features, f, protocol=4)
        expr_p.write_text(expression)

        # Scrubbed environment: no inherited credentials, no proxy, no network helpers.
        env = {
            "PATH": "/usr/bin:/bin",
            "AEAP_ROOT": str(ENGINE_ROOT),
            "PYTHONHASHSEED": "0",
            "HOME": str(tmp),
            "TMPDIR": str(tmp),
            "no_proxy": "*",
        }
        libs = json.dumps([e for e in sys.path if e and os.path.isdir(e)])
        limits_json = json.dumps({
            "cpu_seconds": int(lim["cpu_seconds"]),
            "memory_bytes": int(lim["memory_bytes"]),
            "max_processes": int(lim["max_processes"]),
        })
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _CHILD, limits_json, str(feat_p), str(out_p),
                 str(expr_p), libs],
                capture_output=True, text=True, env=env, cwd=str(tmp),
                timeout=float(lim["wall_seconds"]),
            )
        except subprocess.TimeoutExpired:
            base_manifest.update(status="timeout",
                                 finished_at=datetime.now(TZ).isoformat(timespec="seconds"))
            return None, base_manifest

        if proc.returncode != 0 or not out_p.exists():
            status = "resource_exceeded" if proc.returncode in (-9, -24, -25, 137) else "failed"
            base_manifest.update(
                status=status,
                finished_at=datetime.now(TZ).isoformat(timespec="seconds"),
                stderr=(proc.stderr or "")[-2000:],
            )
            return None, base_manifest

        with out_p.open("rb") as f:
            scores = pickle.load(f)
        try:
            child_report = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
        except Exception:
            child_report = {}

    if not isinstance(scores, pd.Series):
        base_manifest.update(status="failed", stderr="candidate did not return a Series")
        return None, base_manifest

    scores = scores.sort_index()
    if int(len(scores)) > int(lim["max_output_rows"]):
        base_manifest.update(status="resource_exceeded",
                             stderr=f"output rows {len(scores)} exceed max_output_rows")
        return None, base_manifest
    if scores.index.duplicated().any():
        base_manifest.update(status="failed", stderr="output violates (asset, date) key uniqueness")
        return None, base_manifest

    base_manifest["isolation"]["limits_applied"] = child_report.get("limits_applied", {})
    base_manifest["isolation"]["limits_skipped"] = child_report.get("limits_skipped", {})
    base_manifest.update(
        finished_at=datetime.now(TZ).isoformat(timespec="seconds"),
        output_sha256=_sha_obj(scores),
        row_count=int(len(scores)),
        status="ok",
    )
    return scores, base_manifest


def invariance_check(expression: str, features: dict[str, pd.Series],
                     future_features: dict[str, pd.Series], historical_index) -> dict:
    """Append or modify FUTURE rows and require historical scores to be bit-identical.

    This is the empirical look-ahead test: if adding data the candidate should not be able
    to see changes a past score, the candidate reads the future regardless of what its AST
    looked like.
    """
    a, ma = execute(expression, features, candidate_id="INVAR-A",
                    executor_run_id="invariance", firewall_passed=True)
    b, mb = execute(expression, future_features, candidate_id="INVAR-B",
                    executor_run_id="invariance", firewall_passed=True)
    if a is None or b is None:
        return {"status": "INCONCLUSIVE", "reason": "execution failed",
                "manifests": [ma["status"], mb["status"]]}
    ha = a.reindex(historical_index)
    hb = b.reindex(historical_index)
    same = ha.equals(hb) or (ha.isna() == hb.isna()).all() and (
        (ha.dropna() - hb.dropna()).abs().max() == 0)
    return {
        "status": "PASS" if same else "FAIL",
        "reason": "historical scores unchanged after future rows were added"
                  if same else "historical scores CHANGED when future rows were added — look-ahead",
        "historical_rows": int(len(historical_index)),
    }
