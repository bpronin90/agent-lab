"""Offline tests for the Calm Down agent. They validate the pipeline, not live research."""

import json
import re

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_lab import runtime
from agent_lab.agents import REGISTRY
from agent_lab.agents.calm_down import retrieval
from agent_lab.agents.calm_down.mock_script import load_mock_dossier
from agent_lab.agents.calm_down.pipeline import research_settings, run_calm_down
from agent_lab.agents.calm_down.render import render_email_html
from agent_lab.agents.calm_down.retrieval import (
    RetrievalError, SourceRecord, check_url, html_to_text, mode_from_env, research_session, safe_get,
)
from agent_lab.agents.calm_down.schemas import CalmDownDossier
from agent_lab.agents.calm_down.verify import normalize, verify_dossier
from agent_lab.cli import main
from agent_lab.config import Settings

FEAR = "AI will replace lawyers and accountants"
OFFLINE = research_settings(Settings.from_env({}), env={})


def read_all_fixture_sources():
    corpus = retrieval.load_fixture_corpus()
    return {s["source_id"]: SourceRecord(source_id=s["source_id"], title=s["title"], url=s["url"],
                                         publisher=s["publisher"], year=s["year"], tier=s["tier"],
                                         backend="fixture", text=s["text"], read=True)
            for s in corpus["sources"]}


def dossier(**changes):
    data = load_mock_dossier()
    data.update(changes)
    return CalmDownDossier.model_validate(data)


# ---------------------------------------------------------------- end to end (fixture)

def test_fixture_pipeline_produces_labeled_artifacts(tmp_path):
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert result.record.status == "succeeded" and result.ok, result.report.problems
    assert result.record.agent_name == "calm_down"
    assert set(result.record.tools_used) == {"search_sources", "read_source"}
    html = (tmp_path / "email.html").read_text()
    text = (tmp_path / "email.txt").read_text()
    md = (tmp_path / "research-dossier.md").read_text()
    assert "FIXTURE PREVIEW" in html and "FIXTURE PREVIEW" in text and "FIXTURE PREVIEW" in md
    for needle in ("CALM DOWN.", "Doom Meter", "What the Doomers Get Right", "Reasons to Chill", "THE VERDICT"):
        assert needle in html
    provenance = json.loads((tmp_path / "sources.json").read_text())
    assert provenance["mode"] == "fixture" and all(s["text_sha256"] for s in provenance["sources"] if s["read"])


def test_email_html_is_email_safe(tmp_path):
    run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    html = (tmp_path / "email.html").read_text()
    assert "<script" not in html.lower() and "<img" not in html.lower() and "javascript:" not in html.lower()
    assert "<link" not in html.lower() and "@import" not in html
    hrefs = re.findall(r'href="([^"]+)"', html)
    assert hrefs and all(h.startswith("https://") for h in hrefs)
    corpus_urls = {s["url"] for s in retrieval.load_fixture_corpus()["sources"]}
    assert set(hrefs) <= {u.replace("&", "&amp;") for u in corpus_urls}


def test_every_rendered_citation_was_read_and_verified(tmp_path):
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    verified_sources = {sid for f in result.report.verified for sid in f.source_ids}
    provenance = json.loads((tmp_path / "sources.json").read_text())
    read = {s["source_id"] for s in provenance["sources"] if s["read"]}
    assert verified_sources <= read


def test_html_escapes_model_text():
    d = dossier(opening="Cousin <script>alert(x)</script> & friends, calm down please now.")
    report = verify_dossier(d, read_all_fixture_sources())
    html = render_email_html(d, report, read_all_fixture_sources(), fixture=True)
    assert "<script>alert" not in html and "&lt;script&gt;" in html


# ---------------------------------------------------------------- verification rejections

def test_fabricated_quote_is_rejected():
    data = load_mock_dossier()
    data["findings"][3]["evidence_quote"] = "AI chatbots have increased accountant earnings dramatically"
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F4" and "verbatim" in r.reason for r in report.rejected)
    assert not report.ok  # F4 is referenced by reasons_to_chill


def test_unread_source_is_rejected():
    sources = read_all_fixture_sources()
    sources["S6"].read = False
    read = {k: v for k, v in sources.items() if v.read}
    report = verify_dossier(dossier(), read)
    assert any(r.item == "F6" and "not read" in r.reason for r in report.rejected)


def test_unknown_source_id_is_rejected():
    data = load_mock_dossier()
    data["findings"][0]["source_ids"] = ["S99"]
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F1" for r in report.rejected)


def test_claim_cannot_add_statistics():
    data = load_mock_dossier()
    data["findings"][4]["claim"] = "Lawyers were fined because 97 percent of AI citations are fake."
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert any(r.item == "F5" and "97" in r.reason for r in report.rejected)


@pytest.mark.parametrize("field,value,needle", [
    ("verdict", "Lawyers are safe and their jobs will never be automated by anything at all.", "guarantee"),
    ("verdict", "Relax: 99% of lawyers keep their jobs, which is plenty of good news for everyone.", "99"),
    ("opening", "Read https://example.com before panicking about robots taking your law degree.", "URL"),
])
def test_editorial_honesty_rules(field, value, needle):
    report = verify_dossier(dossier(**{field: value}), read_all_fixture_sources())
    assert not report.ok and any(needle in p for p in report.problems)


def test_one_sided_research_is_refused():
    data = load_mock_dossier()
    data["findings"] = [f for f in data["findings"] if f["stance"] == "challenges_fear"]
    data["doomers_get_right"] = []
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert not report.ok and any("one-sided" in p for p in report.problems)


def test_too_few_findings_is_insufficient():
    data = load_mock_dossier()
    data["findings"] = data["findings"][:2]
    report = verify_dossier(CalmDownDossier.model_validate(data), read_all_fixture_sources())
    assert not report.ok


def test_agent_reported_insufficient_blocks_email():
    report = verify_dossier(dossier(status="insufficient"), read_all_fixture_sources())
    assert not report.ok
    with pytest.raises(ValueError):
        render_email_html(dossier(status="insufficient"), report, read_all_fixture_sources(), fixture=True)


def test_normalize_handles_typography():
    assert normalize("“Smart” quotes — and\n spaces.") == normalize('"smart" quotes - and spaces')


# ---------------------------------------------------------------- failure handling end to end

def test_failed_run_writes_limitation_not_email(tmp_path, monkeypatch):
    def broken(messages, info):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(runtime, "build_model", lambda s, m=None: FunctionModel(broken, model_name="x"))
    monkeypatch.setattr(runtime, "_sleep", lambda s: None)
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert result.record.status == "failed" and result.record.error_class == "transient"
    assert not (tmp_path / "email.html").exists() and (tmp_path / "LIMITATION.md").exists()


def test_model_citing_unread_sources_gets_no_email(tmp_path, monkeypatch):
    """A model that skips reading and cites from 'memory' is blocked."""
    def lazy(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, load_mock_dossier())])

    monkeypatch.setattr(runtime, "build_model", lambda s, m=None: FunctionModel(lazy, model_name="lazy"))
    result = run_calm_down(FEAR, OFFLINE, "fixture", tmp_path)
    assert result.record.status == "succeeded" and not result.ok
    assert not (tmp_path / "email.html").exists()
    assert "not read" in (tmp_path / "research-dossier.md").read_text()


def test_cli_calm_down_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_LAB_RUN_LOG_DIR", str(tmp_path / "runs"))
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "ok")]) == 0
    assert (tmp_path / "runs" / "runs.jsonl").exists()
    monkeypatch.setenv("AGENT_LAB_CALM_DOWN_RETRIEVAL", "live")
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "x")]) == 2  # live + mock refused
    monkeypatch.setenv("AGENT_LAB_CALM_DOWN_RETRIEVAL", "bogus")
    assert main(["calm-down", FEAR, "--out", str(tmp_path / "x")]) == 2


# ---------------------------------------------------------------- tools and retrieval safety

def test_tool_budgets_and_unknown_ids():
    from pydantic_ai import ModelRetry
    with research_session("fixture") as session:
        with pytest.raises(ModelRetry):
            retrieval.read_source("S1")  # not listed yet
        for i in range(retrieval.MAX_SEARCHES):
            retrieval.search_sources(f"lawyers {i}")
        with pytest.raises(ModelRetry):
            retrieval.search_sources("one too many")
        with pytest.raises(ModelRetry):
            retrieval.fetch_url("https://evil.example/")
        assert "UNTRUSTED SOURCE TEXT" in retrieval.read_source("S5")
        assert session.read_sources().keys() == {"S5"}


def test_mode_from_env():
    assert mode_from_env({}) == "fixture"
    assert mode_from_env({"AGENT_LAB_CALM_DOWN_RETRIEVAL": "LIVE"}) == "live"
    with pytest.raises(ValueError):
        mode_from_env({"AGENT_LAB_CALM_DOWN_RETRIEVAL": "web"})


@pytest.mark.parametrize("url", [
    "http://www.nber.org/papers/w1",          # not https
    "https://evil.example/",                  # not allowlisted
    "https://nber.org.evil.example/",         # suffix trick
    "https://user:pw@www.nber.org/",          # credentials
    "https://www.nber.org:8443/",             # odd port
    "file:///etc/passwd",
])
def test_check_url_refuses_unsafe(url):
    with pytest.raises(RetrievalError):
        check_url(url, resolve=False)


def test_check_url_allows_subdomains_and_rejects_private_ip(monkeypatch):
    assert check_url("https://export.arxiv.org/abs/1", resolve=False) == "primary"
    monkeypatch.setattr(retrieval.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    with pytest.raises(RetrievalError, match="non-public"):
        check_url("https://www.nber.org/", resolve=True)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_off_allowlist_is_refused():
    client = _mock_client(lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    with pytest.raises(RetrievalError, match="allowlist"):
        safe_get("https://www.nber.org/papers/w1", client=client, resolve=False)


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_FETCH_BYTES", 100)
    client = _mock_client(lambda r: httpx.Response(200, content=b"x" * 500))
    with pytest.raises(RetrievalError, match="size cap"):
        safe_get("https://www.nber.org/papers/w1", client=client, resolve=False)


def test_html_to_text_drops_scripts():
    title, text = html_to_text("<title>T</title><script>steal()</script><p>Hello <b>world</b></p>")
    assert title == "T" and "steal" not in text and "Hello world" in text


def test_openalex_abstract_reconstruction():
    assert retrieval._abstract({"world": [1], "hello": [0]}) == "hello world"


# ---------------------------------------------------------------- runtime compatibility

def test_registry_keeps_demo_and_adds_calm_down():
    assert {"demo", "calm_down"} <= set(REGISTRY)
    assert REGISTRY["calm_down"].thinking_budget < OFFLINE.max_output_tokens


def test_research_settings_respect_explicit_env():
    s = research_settings(Settings.from_env({}), env={"AGENT_LAB_MAX_REQUESTS": "5"})
    assert s.max_requests == Settings().max_requests and s.max_tool_calls == 30
