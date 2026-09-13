"""Shared skip marker driven by the shipped policy's calibration status.

The portable template ships gates.v1 UNCALIBRATED on purpose: thresholds must come from
the adopter's own data, never inherited from ours. Evaluation-dependent tests therefore
SKIP with instructions rather than fail, so a fresh install does not look broken.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def calibration_status() -> str | None:
    try:
        return yaml.safe_load((ROOT / "aeap" / "policies" / "gates.v1.yaml").read_text()
                              ).get("calibration_status")
    except Exception:
        return None


requires_calibration = pytest.mark.skipif(
    calibration_status() == "UNCALIBRATED",
    reason=("gates.v1 is UNCALIBRATED — run `python3 -m aeap.engine.calibrate --freeze` "
            "to derive thresholds from your own data first"),
)
