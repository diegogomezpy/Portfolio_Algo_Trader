"""Unit tests for the premium-deployment-lag quantifier (scripts.backtest_covered_calls).

The live cycle writes calls last, so premium funds the *next* rebalance (lagged); the
quantifier compares that against an ideal same-cycle deployment. The sign of the drag follows
the capped-equity 'core' return: positive core → deploying premium sooner helps; negative core
(upside capped in a bull market) → the lag is marginally better.
"""

from __future__ import annotations

import pandas as pd

from scripts.backtest_covered_calls import premium_deployment_drag


def test_no_premium_means_no_drag():
    df = pd.DataFrame({"cc_net": [0.01, 0.02, -0.01], "premium_income": [0.0, 0.0, 0.0]})
    assert abs(premium_deployment_drag(df)["drag_bps_per_yr"]) < 1e-6


def test_drag_sign_follows_core_return():
    # core = cc_net - premium. Positive core → same-cycle deployment is better (positive drag).
    pos = pd.DataFrame({"cc_net": [0.03, 0.03], "premium_income": [0.01, 0.01]})   # core +0.02
    assert premium_deployment_drag(pos)["drag_bps_per_yr"] > 0
    # Negative core (capped upside) → deploying premium a cycle late is better (negative drag).
    neg = pd.DataFrame({"cc_net": [0.005, 0.005], "premium_income": [0.02, 0.02]})  # core -0.015
    assert premium_deployment_drag(neg)["drag_bps_per_yr"] < 0


def test_empty_frame_is_safe():
    assert premium_deployment_drag(pd.DataFrame()) == {}
