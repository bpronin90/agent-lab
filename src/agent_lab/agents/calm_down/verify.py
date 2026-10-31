"""Citation integrity and editorial honesty checks for a Calm Down dossier.

The rules are deliberately strict. A finding survives only if:

* every cited source was actually READ during this run (not just listed by a search);
* its evidence quote appears verbatim, after whitespace, case and punctuation normalization,
  in the text the model was shown for at least one cited source;
* every number in the claim also appears in that quote (the claim cannot add statistics).

Editorial copy (headline, opening, analogies, doom meter, advice, verdict) may not contain
URLs, guarantees, or numbers that do not appear in verified evidence. The edition is blocked
(status ``insufficient``) when there is not enough verified evidence on both sides.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from agent_lab.agents.calm_down.retrieval import SourceRecord
from agent_lab.agents.calm_down.schemas import CalmDownDossier, Finding

MIN_VERIFIED_FINDINGS = 4
MIN_DISTINCT_SOURCES = 3

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_URL = re.compile(r"(https?://|www\.)", re.I)
_GUARANTEE = re.compile(
    r"\b(guarantee[ds]?|100% safe|completely safe|never (?:be )?(?:replaced|automated)|will never|"
    r"safe forever|forever safe|impossible to replace|no chance)\b",
    re.I,
)
_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    text = re.sub(r"[\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip(" .…")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


@dataclass
class Rejection:
    item: str
    reason: str


@dataclass
class VerificationReport:
    status: str  # complete | insufficient
    verified: list[Finding] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    cited_sources: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def check_finding(finding: Finding, read: dict[str, SourceRecord]) -> str | None:
    """Return a rejection reason, or None if the finding is supported."""
    unread = [sid for sid in finding.source_ids if sid not in read]
    if unread:
        return f"cites sources that were not read in this run: {', '.join(unread)}"
    quote = normalize(finding.evidence_quote)
    if not any(quote in normalize(read[sid].text) for sid in finding.source_ids):
        return "evidence quote does not appear verbatim in any cited source text"
    extra = numbers(finding.claim) - numbers(finding.evidence_quote)
    if extra:
        return f"claim contains numbers not in its evidence quote: {', '.join(sorted(extra))}"
    if not all(read[sid].url.startswith("https://") for sid in finding.source_ids):
        return "cited source has no https URL"
    return None


def editorial_texts(dossier: CalmDownDossier, findings: list[Finding]) -> dict[str, str]:
    texts = {
        "headline": dossier.headline, "subtitle": dossier.subtitle, "opening": dossier.opening,
        "doom_meter.label": dossier.doom_meter.label, "doom_meter.rationale": dossier.doom_meter.rationale,
        "verdict": dossier.verdict,
    }
    for i, item in enumerate(dossier.adapt):
        texts[f"adapt[{i}]"] = item.text
    for f in findings:
        if f.analogy:
            texts[f"{f.id}.analogy"] = f.analogy
    return texts


def verify_dossier(dossier: CalmDownDossier, read: dict[str, SourceRecord]) -> VerificationReport:
    report = VerificationReport(status="complete")
    for finding in dossier.findings:
        reason = check_finding(finding, read)
        if reason:
            report.rejected.append(Rejection(finding.id, reason))
        else:
            report.verified.append(finding)
    verified_ids = {f.id for f in report.verified}

    allowed_numbers: set[str] = set()
    for f in report.verified:
        allowed_numbers |= numbers(f.evidence_quote)
    allowed_numbers |= {str(dossier.doom_meter.score), "10"}  # "3/10" is the meter itself

    for name, text in editorial_texts(dossier, report.verified).items():
        if _URL.search(text):
            report.problems.append(f"{name}: editorial text may not contain URLs")
        if _GUARANTEE.search(text):
            report.problems.append(f"{name}: makes a guarantee the evidence cannot support")
        invented = numbers(text) - allowed_numbers
        if invented:
            report.problems.append(f"{name}: numbers not found in verified evidence: {', '.join(sorted(invented))}")

    for section in ("doomers_get_right", "reasons_to_chill"):
        missing = [fid for fid in getattr(dossier, section) if fid not in verified_ids]
        if missing:
            report.problems.append(f"{section} references unverified findings: {', '.join(missing)}")
    for i, item in enumerate(dossier.adapt):
        if not set(item.finding_ids) <= verified_ids:
            report.problems.append(f"adapt[{i}] references unverified findings")

    stances = {f.stance for f in report.verified}
    sources = {sid for f in report.verified for sid in f.source_ids}
    report.cited_sources = sorted(sources)
    if dossier.status != "complete":
        report.problems.append("the agent itself reported that research was insufficient")
    if len(report.verified) < MIN_VERIFIED_FINDINGS:
        report.problems.append(f"only {len(report.verified)} verified findings (need {MIN_VERIFIED_FINDINGS})")
    if len(sources) < MIN_DISTINCT_SOURCES:
        report.problems.append(f"only {len(sources)} distinct verified sources (need {MIN_DISTINCT_SOURCES})")
    if not stances & {"supports_fear", "mixed"}:
        report.problems.append("no verified evidence supporting the fear: one-sided research is refused")
    if "challenges_fear" not in stances:
        report.problems.append("no verified evidence challenging the fear")
    if not dossier.doomers_get_right or not dossier.reasons_to_chill:
        report.problems.append("both 'What the Doomers Get Right' and 'Reasons to Chill' must be populated")
    if report.problems:
        report.status = "insufficient"
    return report
