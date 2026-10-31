"""Citation integrity, editorial honesty, and the human support gate for a Calm Down dossier.

Two layers, and both must pass before an email is rendered.

1. Mechanical checks (this module). A finding is *quote-verified* only if:
   * every cited source was READ during this run, and EVERY cited source contains the quote
     (padding a finding with unrelated read sources is rejected);
   * the quote appears verbatim (after whitespace, case and punctuation normalization);
   * every number in the claim appears in the quote;
   * the claim and the quote agree on negation (a reversed claim is rejected).
   Every model-written field that reaches the email is also checked: no URLs, no guarantees,
   no numbers in global editorial text, and numbers in a finding's caveat or analogy only if they
   appear in that finding's own quote. The opposing sections must hold the stance they claim, and
   source diversity counts distinct works (by URL), not ids.

2. Human support review. Substring checks cannot prove that a claim fairly represents its
   evidence (population, outcome, quantitative context). So the pipeline fails closed: each
   finding's claim/quote pair, and the editorial copy as a whole, must be approved by a person,
   matched by content hash. Changing any approved text invalidates the approval.
   Until then the status is ``needs_review`` and no email is rendered.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

from agent_lab.agents.calm_down.retrieval import SourceRecord
from agent_lab.agents.calm_down.schemas import CalmDownDossier, Finding

MIN_VERIFIED_FINDINGS = 4
MIN_DISTINCT_SOURCES = 3

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|gov|edu|io|ai|example)\b)", re.I)
_GUARANTEE = re.compile(
    r"\b(guarantee[ds]?|100% safe|completely safe|totally safe|never (?:be )?(?:replaced|automated)|will never|"
    r"safe forever|forever safe|are safe|is safe|impossible to replace|no chance|proves?|proven)\b",
    re.I,
)
_NEGATION = re.compile(r"\b(no|not|none|never|nothing|neither|nor|cannot|fail(?:s|ed)?\s+to)\b|n't\b", re.I)
_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                            "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    text = re.sub(r"[\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip(" .…")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def source_digest(rec: SourceRecord) -> str:
    """Content identity of a source: digest of the exact text the model saw (recomputed, not trusted)."""
    return hashlib.sha256(rec.text.encode()).hexdigest()


def finding_hash(f: Finding, read: dict[str, SourceRecord]) -> str:
    """Binds a human approval to everything the reader is told about a finding, AND to the evidence:
    claim, quote, stance, kind, confidence, caveat, analogy, the surprising flag, and each cited source's
    URL and text digest. Any change, including a changed source snapshot, invalidates the approval."""
    evidence = [f"{sid}|{read[sid].url}|{source_digest(read[sid])}" if sid in read else f"{sid}|unread"
                for sid in f.source_ids]
    return _sha(f.id, f.claim, f.evidence_quote, ",".join(evidence), f.stance, f.evidence_kind, f.confidence,
                f.caveat, f.analogy or "", str(f.surprising))


@dataclass
class Rejection:
    item: str
    reason: str


@dataclass
class VerificationReport:
    status: str  # complete | needs_review | insufficient
    verified: list[Finding] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    pending_review: list[str] = field(default_factory=list)
    cited_sources: list[str] = field(default_factory=list)
    distinct_works: int = 0
    reviewer: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def _norm_url(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.lower()).rstrip("/")


def check_finding(finding: Finding, read: dict[str, SourceRecord]) -> str | None:
    """Return a rejection reason, or None if the finding passes every mechanical check."""
    unread = [sid for sid in finding.source_ids if sid not in read]
    if unread:
        return f"cites sources that were not read in this run: {', '.join(unread)}"
    quote = normalize(finding.evidence_quote)
    missing = [sid for sid in finding.source_ids if quote not in normalize(read[sid].text)]
    if len(missing) == len(finding.source_ids):
        return "evidence quote does not appear verbatim in any cited source text"
    if missing:
        return f"evidence quote does not appear in cited sources {', '.join(missing)} (unsupported citation)"
    extra = numbers(finding.claim) - numbers(finding.evidence_quote)
    if extra:
        return f"claim contains numbers not in its evidence quote: {', '.join(sorted(extra))}"
    if bool(_NEGATION.search(finding.claim)) != bool(_NEGATION.search(finding.evidence_quote)):
        return "claim and evidence quote disagree on negation (possible reversed claim)"
    if not all(read[sid].url.startswith("https://") for sid in finding.source_ids):
        return "cited source has no https URL"
    return None


def global_editorial(dossier: CalmDownDossier) -> dict[str, str]:
    texts = {
        "headline": dossier.headline, "subtitle": dossier.subtitle, "opening": dossier.opening,
        "doom_meter.label": dossier.doom_meter.label, "doom_meter.rationale": dossier.doom_meter.rationale,
        "verdict": dossier.verdict,
    }
    texts.update({f"adapt[{i}]": a.text for i, a in enumerate(dossier.adapt)})
    texts.update({f"uncertainties[{i}]": u for i, u in enumerate(dossier.uncertainties)})
    texts.update({f"limitations[{i}]": u for i, u in enumerate(dossier.limitations)})
    return texts


def editorial_hash(dossier: CalmDownDossier) -> str:
    return _sha(json.dumps(global_editorial(dossier), sort_keys=True), str(dossier.doom_meter.score),
                json.dumps([dossier.doomers_get_right, dossier.reasons_to_chill,
                            [a.finding_ids for a in dossier.adapt]]))


def _check_text(name: str, text: str, allowed_numbers: set[str], problems: list[str]) -> None:
    if _URL.search(text):
        problems.append(f"{name}: model-written text may not contain URLs or domains")
    if _GUARANTEE.search(text):
        problems.append(f"{name}: makes a guarantee or proof claim the evidence cannot support")
    invented = numbers(text) - allowed_numbers
    if invented:
        problems.append(f"{name}: contains numbers not allowed here: {', '.join(sorted(invented))}")


def review_request(dossier: CalmDownDossier, report: VerificationReport, read: dict[str, SourceRecord]) -> dict:
    """What a human reviewer must check. Copy approved hashes into a review file."""
    return {
        "instructions": "For each item, confirm the claim is fairly supported by the quote and source "
                        "(population, outcome, direction, and quantitative context). Copy the hash of each item "
                        "you approve into review.json under approvals, with your name as reviewer.",
        "findings": [{"id": f.id, "hash": finding_hash(f, read), "claim": f.claim, "evidence_quote": f.evidence_quote,
                      "stance": f.stance, "kind": f.evidence_kind, "confidence": f.confidence,
                      "surprising": f.surprising, "caveat": f.caveat, "analogy": f.analogy,
                      "sources": [{"id": sid, "title": read[sid].title, "url": read[sid].url,
                                   "text_sha256": source_digest(read[sid]),
                                   "backend": read[sid].backend} for sid in f.source_ids]}
                     for f in report.verified],
        "editorial": {"hash": editorial_hash(dossier), "texts": global_editorial(dossier),
                      "presentation": {"doom_meter_score": dossier.doom_meter.score,
                                       "doomers_get_right": dossier.doomers_get_right,
                                       "reasons_to_chill": dossier.reasons_to_chill,
                                       "advice_evidence": {f"adapt[{i}]": a.finding_ids
                                                           for i, a in enumerate(dossier.adapt)}}},
    }


def verify_dossier(dossier: CalmDownDossier, read: dict[str, SourceRecord], review: dict | None = None) -> VerificationReport:
    report = VerificationReport(status="complete")
    for finding in dossier.findings:
        reason = check_finding(finding, read)
        if reason:
            report.rejected.append(Rejection(finding.id, reason))
        else:
            report.verified.append(finding)
    by_id = {f.id: f for f in report.verified}

    # Every model-written field that reaches the email.
    for name, text in global_editorial(dossier).items():
        _check_text(name, text, set(), report.problems)
    for f in report.verified:
        _check_text(f"{f.id}.caveat", f.caveat, numbers(f.evidence_quote), report.problems)
        if f.analogy:
            _check_text(f"{f.id}.analogy", f.analogy, numbers(f.evidence_quote), report.problems)

    # Opposing sections must contain what they claim to contain.
    for section, allowed, required in (("doomers_get_right", {"supports_fear", "mixed"}, "supports_fear"),
                                       ("reasons_to_chill", {"challenges_fear", "mixed"}, "challenges_fear")):
        ids = getattr(dossier, section)
        missing = [fid for fid in ids if fid not in by_id]
        if missing:
            report.problems.append(f"{section} references unverified findings: {', '.join(missing)}")
        wrong = [fid for fid in ids if fid in by_id and by_id[fid].stance not in allowed]
        if wrong:
            report.problems.append(f"{section} contains findings with the wrong stance: {', '.join(wrong)}")
        if not any(fid in by_id and by_id[fid].stance == required for fid in ids):
            report.problems.append(f"{section} needs at least one verified {required} finding")
    overlap = set(dossier.doomers_get_right) & set(dossier.reasons_to_chill)
    if overlap:
        report.problems.append(f"the same finding appears on both sides: {', '.join(sorted(overlap))}")
    for i, item in enumerate(dossier.adapt):
        if not set(item.finding_ids) <= set(by_id):
            report.problems.append(f"adapt[{i}] references unverified findings")

    works = {_norm_url(read[sid].url) for f in report.verified for sid in f.source_ids}
    report.cited_sources = sorted({sid for f in report.verified for sid in f.source_ids})
    report.distinct_works = len(works)
    stances = {f.stance for f in report.verified}
    if dossier.status != "complete":
        report.problems.append("the agent itself reported that research was insufficient")
    if len(report.verified) < MIN_VERIFIED_FINDINGS:
        report.problems.append(f"only {len(report.verified)} verified findings (need {MIN_VERIFIED_FINDINGS})")
    if len(works) < MIN_DISTINCT_SOURCES:
        report.problems.append(f"only {len(works)} distinct verified works (need {MIN_DISTINCT_SOURCES})")
    if not stances & {"supports_fear", "mixed"}:
        report.problems.append("no verified evidence supporting the fear: one-sided research is refused")
    if "challenges_fear" not in stances:
        report.problems.append("no verified evidence challenging the fear")
    if report.problems:
        report.status = "insufficient"
        return report

    # Human support gate: fail closed until every displayed claim and the editorial copy are approved.
    approvals = set((review or {}).get("approvals", []))
    report.reviewer = (review or {}).get("reviewer")
    report.pending_review = [f.id for f in report.verified if finding_hash(f, read) not in approvals]
    if editorial_hash(dossier) not in approvals:
        report.pending_review.append("editorial")
    if report.pending_review or not report.reviewer:
        report.status = "needs_review"
    return report
