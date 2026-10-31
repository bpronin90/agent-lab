"""End-to-end Calm Down run: bounded research run, verification, then rendering.

The model run goes through the standard `runtime.run_agent`, so retries, the shared usage
budget, error classes and the RunRecord stay exactly the same as for every other agent.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from agent_lab.agents.calm_down import AGENT_NAME
from agent_lab.agents.calm_down.render import render_dossier_md, render_email_html, render_email_text
from agent_lab.agents.calm_down.retrieval import RetrievalMode, research_session
from agent_lab.agents.calm_down.schemas import CalmDownDossier
from agent_lab.agents.calm_down.verify import VerificationReport, review_request, verify_dossier
from agent_lab.config import Settings
from agent_lab.runtime import run_agent
from agent_lab.schemas import RunRecord

# A research run needs more room than the demo. These apply only when the caller did not set them.
RESEARCH_DEFAULTS = {
    "AGENT_LAB_MAX_REQUESTS": ("max_requests", 16),
    "AGENT_LAB_MAX_TOOL_CALLS": ("max_tool_calls", 30),
    "AGENT_LAB_MAX_OUTPUT_TOKENS": ("max_output_tokens", 8192),
    "AGENT_LAB_DEADLINE_SECONDS": ("deadline_seconds", 300),
}


def research_settings(settings: Settings, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    overrides = {field: value for key, (field, value) in RESEARCH_DEFAULTS.items() if not env.get(key)}
    return dataclasses.replace(settings, **overrides)


@dataclass
class CalmDownResult:
    record: RunRecord
    mode: RetrievalMode
    report: VerificationReport | None
    dossier: CalmDownDossier | None
    files: dict[str, Path]

    @property
    def ok(self) -> bool:
        return self.record.status == "succeeded" and self.report is not None and self.report.ok


def build_prompt(fear: str) -> str:
    return (f"The fear to investigate: {fear.strip()}\n"
            "Research it on both sides, verify, then write the Calm Down edition.")


# Every file this pipeline may write. They are removed before each run so that a failed run
# can never leave an earlier run's email behind under the documented names.
GENERATED_FILES = ("email.html", "email.txt", "research-dossier.md", "LIMITATION.md", "sources.json",
                   "review-request.json")


def clear_generated(out_dir: Path) -> None:
    for name in GENERATED_FILES:
        path = out_dir / name
        if path.is_file() or path.is_symlink():
            path.unlink()


def fixture_review() -> dict:
    """The authoring agent's own, non-independent approval of the fixture preview's claims."""
    text = resources.files("agent_lab.agents.calm_down.fixtures").joinpath("fixture_review.json").read_text("utf-8")
    return json.loads(text)


def load_review(path: Path | None) -> dict | None:
    if path is None:
        return None
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("approvals", []), list):
        raise ValueError("review file must be a JSON object with a reviewer and an approvals list")
    return data


def run_calm_down(fear: str, settings: Settings, mode: RetrievalMode, out_dir: Path,
                  review: dict | None = None) -> CalmDownResult:
    if mode == "live" and settings.provider == "mock":
        raise ValueError("live retrieval with the mock provider would mix fixture content with live sources")
    if review is not None and mode == "live" and review.get("scope") == "fixture":
        raise ValueError("a fixture-scoped review cannot approve a live edition; a person must review it")
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_generated(out_dir)
    with research_session(mode) as session:
        record = run_agent(build_prompt(fear), settings, AGENT_NAME)
    sources = session.sources
    files: dict[str, Path] = {}
    meta = {"run_id": record.run_id, "status": record.status, "provider": record.provider,
            "served_model": record.served_model, "git_sha": record.git_sha, "config_hash": record.config_hash[:16],
            "requests": record.requests, "tool_calls": len(record.tool_calls), "searches": len(session.searches)}

    dossier = report = None
    if record.status == "succeeded" and record.output is not None:
        try:
            dossier = CalmDownDossier.model_validate(record.output)
        except ValidationError as exc:  # defensive: runtime already validated
            report = VerificationReport(status="insufficient", problems=[f"output failed validation: {exc}"])
    else:
        report = VerificationReport(status="insufficient",
                                    problems=[f"research run failed ({record.error_class}): {record.error}"])
    if dossier is not None:
        report = verify_dossier(dossier, session.read_sources(), review)
    assert report is not None
    if session.errors:
        report.problems.extend(f"retrieval error (non-fatal): {e}" for e in session.errors)

    fixture = mode == "fixture"
    if dossier is not None:
        files["dossier"] = out_dir / "research-dossier.md"
        files["dossier"].write_text(render_dossier_md(dossier, report, sources, mode, meta), "utf-8")
    if dossier is not None and report.ok:
        files["html"] = out_dir / "email.html"
        files["html"].write_text(render_email_html(dossier, report, sources, fixture), "utf-8")
        files["text"] = out_dir / "email.txt"
        files["text"].write_text(render_email_text(dossier, report, sources, fixture), "utf-8")
    elif dossier is not None and report.status == "needs_review":
        files["review_request"] = out_dir / "review-request.json"
        files["review_request"].write_text(json.dumps(review_request(dossier, report), indent=2), "utf-8")
        files["limitation"] = out_dir / "LIMITATION.md"
        files["limitation"].write_text(
            "# Calm Down is waiting for human support review\n\nThe quotes were found in the retrieved sources, "
            "but a machine cannot prove that each claim fairly represents its evidence. No email was produced. "
            "Review `review-request.json`, then rerun with `--review review.json`.\n\nPending: "
            + ", ".join(report.pending_review) + "\n", "utf-8")
    else:
        files["limitation"] = out_dir / "LIMITATION.md"
        files["limitation"].write_text(
            "# Calm Down could not finish reliable research\n\nNo reassurance email was produced, because "
            "producing one would mean making claims without verified evidence.\n\n"
            + "\n".join(f"- {p}" for p in report.problems) + "\n", "utf-8")
    files["provenance"] = out_dir / "sources.json"
    files["provenance"].write_text(json.dumps(
        {"mode": mode, "searches": session.searches, "sources": [s.provenance() for s in sources.values()]},
        indent=2), "utf-8")
    return CalmDownResult(record=record, mode=mode, report=report, dossier=dossier, files=files)
