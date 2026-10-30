"""Integration contract between Agent Lab and the existing Fantasy GM repository.

Phase 1 defines the boundary only; nothing here imports or modifies Fantasy GM.
Fantasy GM (producer) emits a ResearchPackEnvelope built from its existing
Research Pack output. Agent Lab (consumer) returns an AdvisoryDecision.
See docs/integration/fantasy-gm-contract.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"


class RosterPlayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    team: str = Field(pattern=r"^[A-Z]{2,3}$", description="Uppercase NFL abbreviation.")
    position: Literal["QB", "RB", "WR", "TE", "K", "DEF"]
    slot: str = Field(description="Assigned slot: QB, RB, WR, TE, FLEX, SUPER_FLEX, K, DEF, BN, IR, IR+.")
    matchup: str | None = Field(default=None, description="Opponent, 'BYE', or null when unknown.")
    player_id: str | None = None


class ResearchPackEnvelope(BaseModel):
    """Input: what Fantasy GM hands to an agent."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    league_id: str
    season: int = Field(ge=2000, le=2100)
    week: int = Field(ge=0, le=23)
    captured_at: datetime
    is_sample: bool = Field(description="True for synthetic fixtures. Agents must say so in their output.")
    scoring_verified: bool = False
    roster: list[RosterPlayer]
    waivers: list[RosterPlayer] = Field(default_factory=list)
    unfilled_slots: list[str] = Field(default_factory=list)
    question: str = Field(min_length=1, max_length=2000)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "sit", "add", "drop", "hold", "investigate"]
    player: str
    rationale: str
    requires_verification: list[str] = Field(
        default_factory=list, description="Facts (injuries, eligibility, scoring) a human must check first."
    )


class AdvisoryDecision(BaseModel):
    """Output: what an agent hands back. Advisory only; never executed automatically."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    advisory_only: Literal[True] = True
    based_on_sample_data: bool
    summary: str
    recommendations: list[Recommendation] = Field(max_length=20)
    confidence: Literal["low", "medium", "high"]
