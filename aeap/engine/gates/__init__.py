"""The nine gates. Each module exposes run(ctx) -> Check."""
from . import (duplicate, self_collapse, coverage, rank_ic, regime_sign,
               novelty, partial_ic, fama_macbeth, sign_alignment)  # noqa: F401

ORDER = [
    ("G1_duplicate", duplicate),
    ("G2_self_collapse", self_collapse),
    ("G3_coverage", coverage),
    ("G4_rank_ic", rank_ic),
    ("G5_regime_sign", regime_sign),
    ("G6_novelty", novelty),
    ("G7_partial_ic", partial_ic),
    ("G8_fama_macbeth", fama_macbeth),
    ("G9_sign_alignment", sign_alignment),
]
