"""Structured research output for the Calm Down agent.

The model fills `CalmDownDossier`. Nothing in it is trusted until `verify.verify_dossier`
has checked every citation against the sources actually retrieved during the run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

EvidenceKind = Literal["demonstrated_fact", "forecast", "speculation"]
Stance = Literal["supports_fear", "challenges_fear", "mixed"]
Confidence = Literal["low", "medium", "high"]


class Finding(BaseModel):
    id: str = Field(pattern=r"^F\d{1,2}$", description="Stable id such as F1.")
    claim: str = Field(min_length=10, max_length=400, description="What the evidence shows, stated plainly.")
    evidence_kind: EvidenceKind = Field(
        description="demonstrated_fact = measured/observed; forecast = projection or exposure estimate; "
        "speculation = argument without direct measurement."
    )
    stance: Stance = Field(description="Does this support the fear, challenge it, or both?")
    source_ids: list[str] = Field(min_length=1, max_length=3, description="Ids of sources you READ with read_source.")
    evidence_quote: str = Field(
        min_length=15, max_length=500, description="An exact, verbatim excerpt from a cited source's text."
    )
    confidence: Confidence
    caveat: str = Field(min_length=5, max_length=300, description="Main limitation of this evidence.")
    analogy: str | None = Field(default=None, max_length=300, description="Optional funny, accurate analogy.")
    surprising: bool = False


class DoomMeter(BaseModel):
    score: int = Field(ge=0, le=10, description="Subjective doom, 0 (nap) to 10 (bunker).")
    label: str = Field(min_length=2, max_length=60)
    rationale: str = Field(min_length=10, max_length=300)


class AdviceItem(BaseModel):
    text: str = Field(min_length=10, max_length=300)
    finding_ids: list[str] = Field(min_length=1, max_length=4)


class CalmDownDossier(BaseModel):
    fear: str = Field(min_length=5, max_length=300)
    status: Literal["complete", "insufficient"] = Field(
        description="Use insufficient if you could not find and read credible evidence on BOTH sides."
    )
    headline: str = Field(min_length=3, max_length=80)
    subtitle: str = Field(min_length=3, max_length=140)
    opening: str = Field(min_length=20, max_length=900, description="Funny, dramatic, factual opening.")
    doom_meter: DoomMeter
    findings: list[Finding] = Field(max_length=16)
    doomers_get_right: list[str] = Field(default_factory=list, description="Finding ids. Strongest case FOR the fear.")
    reasons_to_chill: list[str] = Field(default_factory=list, description="Finding ids. Strongest case AGAINST it.")
    adapt: list[AdviceItem] = Field(default_factory=list, max_length=6)
    verdict: str = Field(min_length=20, max_length=600)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("findings")
    @classmethod
    def _unique_ids(cls, findings: list[Finding]) -> list[Finding]:
        ids = [f.id for f in findings]
        if len(ids) != len(set(ids)):
            raise ValueError("finding ids must be unique")
        return findings
