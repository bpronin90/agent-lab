# Experiment 3: Calm Down

> "Last Week Tonight meets scientific research meets a mildly sarcastic friend telling you to calm down."

`calm_down` is a research agent. It takes a fear about AI, looks for evidence on **both** sides,
checks every citation against the text it actually retrieved, and only then writes a one-off,
playful HTML email. It writes three files: the email, a plain-text version, and a research dossier.
Nothing is ever sent. Delivery is a human copying the file into their own mail client.

First edition: **CALM DOWN.** *The Robots Are Not Taking Your Law Degree.*

## How it works

```
fear ──► runtime.run_agent("calm_down")  (same bounds, retries, error classes, RunRecord)
           │  tools: search_sources · read_source · fetch_url   (ResearchSession records provenance)
           ▼
       CalmDownDossier (Pydantic)  ──► verify_dossier()  ──► complete?  ──► email.html + email.txt
                                                         └─ insufficient ─► LIMITATION.md (no email)
       research-dossier.md + sources.json are always written
```

| File | Role |
| --- | --- |
| `src/agent_lab/agents/calm_down/schemas.py` | Findings carry an evidence kind (`demonstrated_fact` / `forecast` / `speculation`), a stance, a verbatim quote, and a caveat. |
| `retrieval.py` | Fixture or live backends, budgets, allowlist, and fetch safety. |
| `verify.py` | Citation integrity and editorial honesty rules. |
| `render.py` | Deterministic, escaped, email-safe HTML; plain text; Markdown dossier. |
| `pipeline.py` | Ties the run, verification and rendering together. |
| `fixtures/` | Hand-written offline corpus and the mock researcher's draft. **Synthetic.** |

### What verification enforces

Two layers. Both must pass before an email is rendered.

**1. Mechanical checks (`verify.py`).**
- A cited source must have been **read** during this run. Being listed by a search is not enough.
- The evidence quote must appear verbatim in the text shown to the model, in **every** cited source. The match ignores case, whitespace and typographic quotes. Padding a finding with unrelated sources is rejected.
- A claim may not contain a number that is not in its quote. A claim and its quote must agree on negation, so a reversed claim is rejected.
- These rules cover every model-written field that reaches the email: headline, opening, doom meter, advice, verdict, uncertainties, limitations, caveats and analogies.
  - No URLs or domains.
  - No guarantees or proof claims ("safe forever", "proves").
  - No numbers in global editorial copy.
  - A caveat or analogy may reuse only numbers from its own finding's quote.
- "What the Doomers Get Right" may hold only supporting or mixed findings and needs at least one supporting one. "Reasons to Chill" may hold only challenging or mixed findings and needs at least one challenging one. No finding may appear in both.
- The edition needs at least 4 verified findings and at least 3 distinct **works**, counted by URL rather than by id, with verified evidence on both sides.
- If any rule fails, the status is `insufficient`. No email is written, only `LIMITATION.md` and the dossier (exit 4).

**2. Human support review, as a separate offline step.** Matching substrings cannot prove that a claim fairly represents its evidence: its population, outcome, direction, or quantitative context. So the pipeline fails closed.

- `agent-lab calm-down` does **research only**. It never writes an email. It saves:
  - `bundle.json`: the dossier plus a snapshot of every retrieved source text and its SHA-256.
  - `review-request.json`: everything the reviewer is approving. For each finding, the claim, quote, stance, kind, **confidence**, the surprising flag, caveat, analogy, and each cited source's URL and text digest. For the edition as a whole, the editorial copy plus its presentation: doom-meter score, section selections, and which advice cites which finding.
  - It exits 5 (`needs_review`).
- A person approves hashes in a review file.
- `agent-lab calm-down-finalize <dir> --review review.json` loads that **exact** bundle, recomputes and checks the source digests, applies the approvals, and renders. It never calls a model or the network, so the draft that was reviewed is the draft that gets rendered.
- Each finding's approval hash covers everything shown about the finding and its evidence snapshot: claim, quote, stance, kind, confidence, caveat, analogy, surprising flag, and each cited source's URL and text digest. Changing any of these, including the source text, invalidates the approval. Editing a source's text without updating its digest makes finalize refuse the bundle.
- To research again, run `calm-down` again. That produces a new bundle, which needs a new review.

`--review fixture` uses the packaged fixture self-review. It was written by the authoring agent, is not independent, and covers the offline preview only. Finalize refuses it for a live bundle.

Research first deletes every file this pipeline generates in the output directory (`email.html`, `email.txt`, `research-dossier.md`, `LIMITATION.md`, `sources.json`, `review-request.json`, `bundle.json`). Finalize first deletes `email.html`, `email.txt` and `LIMITATION.md`. So a failed step can never leave an earlier email behind. Other files are untouched.

## Running it

```sh
# Offline fixture preview (default): mock provider + hand-written corpus. No network.
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-out    # exit 5: needs review
agent-lab calm-down-finalize calm-down-out --review fixture                            # offline render

# Live edition: explicit opt-in to BOTH a real model and real retrieval.
export AGENT_LAB_PROVIDER=gemini GEMINI_API_KEY=...        # free tier; never commit
export AGENT_LAB_CALM_DOWN_RETRIEVAL=live
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-live   # exit 5: review
# a person reviews calm-down-live/review-request.json, writes review.json, then (offline, no model call):
agent-lab calm-down-finalize calm-down-live --review review.json
```

Live retrieval with the mock provider is refused, so fixture text can never be mixed into a live edition.
Unless you set them, research runs use larger bounds: 16 requests, 30 tool calls, 8192 output tokens, and a 300 s deadline.

Exit codes: `0` edition rendered (finalize), `4` research insufficient, `5` waiting for human support review (dossier only in both cases), and otherwise the usual runtime codes (`2` config, `3` transient, `1` other).

## Live retrieval safety

- Search uses the free [OpenAlex](https://openalex.org) works API (no key). Its abstracts become the source text.
- `fetch_url` accepts only HTTPS, standard port, no embedded credentials, and an allowlist of research, government, court and professional hosts (`ALLOWED_HOSTS`). Every redirect hop is re-checked. Hosts that resolve to non-public IPs are refused.
- Caps: 8 searches, 14 reads, 5 results per search, 2 MB per response, 15 s timeout, 6000 characters of text per source. PDFs are not parsed.
- No cookies or auth headers are sent. Source text is wrapped in untrusted-data markers, and the instructions tell the model to ignore instructions inside it.
- Provenance (`sources.json`) records URL, backend, retrieval time and the SHA-256 of the exact text the model saw.

## Fixture preview vs live research

`examples/calm-down/` holds the **fixture preview**. Its source excerpts were written by hand from memory
to paraphrase real public works. They are **not verified quotations**, and their numbers must be checked
against the linked sources. Passing verification there only shows that the pipeline works. The email, text and dossier
all carry a FIXTURE PREVIEW banner. The offline tests do not validate live research.

Some fixture figures are older vintages: newer releases of the Stanford payroll study and the BLS outlook report different numbers, and the legal-hallucination figures are pinned to arXiv v1. A live edition has not been produced yet. It needs owner authorization (live Gemini runs spend free-tier quota),
a `GEMINI_API_KEY`, and network egress to OpenAlex and the allowlisted hosts.

## Kubernetes / n8n

No new infrastructure is needed. The existing image and Job pattern work: override the container args with
`["calm-down", "<fear>", "--out", "/data/runs/calm-down"]`, and collect the files from the runs volume.
Nothing here schedules runs or sends email.
