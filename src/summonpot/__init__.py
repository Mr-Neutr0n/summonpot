"""Modernize APIs for AI with exact behavior and bounded agent-owned decisions."""

from importlib.metadata import PackageNotFoundError, version

from pydantic_ai.usage import UsageLimits

from summonpot._context import AuthenticationError
from summonpot.contracts import (
    AgentChoice,
    AtLeast,
    AtMost,
    Between,
    CallBounds,
    Exactly,
    FromContext,
    FromRequest,
    FromResult,
    Operation,
)
from summonpot.dependencies import Depends, Required
from summonpot.summon import Summon

try:
    __version__ = version("summonpot")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "AgentChoice",
    "AtLeast",
    "AtMost",
    "AuthenticationError",
    "Between",
    "CallBounds",
    "Depends",
    "Exactly",
    "FromContext",
    "FromRequest",
    "FromResult",
    "Operation",
    "Required",
    "Summon",
    "UsageLimits",
]
