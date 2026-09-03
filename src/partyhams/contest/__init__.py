"""Pluggable contest engine.

A contest is a data-driven :class:`~partyhams.contest.base.ContestDefinition`.
Adding a new contest means writing one of these — no changes to the logging core.
Modules self-register via :func:`~partyhams.contest.registry.register`.
"""

# Importing the module registers it.
from partyhams.contest import fieldday as _fieldday  # noqa: F401
from partyhams.contest import general as _general  # noqa: F401
from partyhams.contest import pota as _pota  # noqa: F401
from partyhams.contest.base import (
    ContestConfig,
    ContestDefinition,
    ExchangeField,
    ScoreSummary,
)
from partyhams.contest.registry import available, get, register

__all__ = [
    "ContestConfig",
    "ContestDefinition",
    "ExchangeField",
    "ScoreSummary",
    "available",
    "get",
    "register",
]
