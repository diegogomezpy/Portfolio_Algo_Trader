"""Strategy plugins. ``register_all()`` populates the registry (called by the orchestrator, A3).

Kept as an explicit call rather than import-time side effects, so importing the package is free of
surprises and tests control registration.
"""

from __future__ import annotations

from engine import strategy as _S
from engine.strategies.low_beta_overwrite import LowBetaOverwrite

__all__ = ["LowBetaOverwrite", "register_all"]


def register_all() -> None:
    """Register every built-in strategy. Idempotent — safe to call on every process start."""
    if not _S.is_registered(LowBetaOverwrite.name):
        _S.register(LowBetaOverwrite())
