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

- A cited source must have been **read** during this run (`read_source`/`fetch_url`). Being listed by a search is not enough.
- The evidence quote must appear verbatim in the text the model was shown. The match ignores case, whitespace and typographic quotes.
- A claim may not contain a number that is not in its quote.
- Editorial copy (headline, opening, analogies, doom meter, advice, verdict) may not contain URLs, guarantees ("will never be automated", "safe forever"), or numbers absent from verified evidence.
- The edition must have at least 4 verified findings from at least 3 sources, verified evidence both supporting and challenging the fear, and populated "Doomers Get Right" and "Reasons to Chill" sections.
- If any rule fails, the email is not rendered. The dossier and `LIMITATION.md` explain why (CLI exit code 4).

These checks prove that quotes are **present in retrieved text**. They cannot prove that a source is correct, or that the model's paraphrase in `claim` is fair. That judgment is left to the dossier's reader.

## Running it

```sh
# Offline fixture preview (default): mock provider + hand-written corpus. No network.
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-out

# Live edition: explicit opt-in to BOTH a real model and real retrieval.
export AGENT_LAB_PROVIDER=gemini GEMINI_API_KEY=...        # free tier; never commit
export AGENT_LAB_CALM_DOWN_RETRIEVAL=live
agent-lab calm-down "AI will replace lawyers and accountants" --out calm-down-live
```

Live retrieval with the mock provider is refused, so fixture text can never be mixed into a live edition.
Unless you set them, research runs use larger bounds: 16 requests, 30 tool calls, 8192 output tokens, and a 300 s deadline.

Exit codes: `0` edition rendered, `4` research insufficient (dossier only), and otherwise the usual runtime codes (`2` config, `3` transient, `1` other).

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

A live edition has not been produced yet. It needs owner authorization (live Gemini runs spend free-tier quota),
a `GEMINI_API_KEY`, and network egress to OpenAlex and the allowlisted hosts.

## Kubernetes / n8n

No new infrastructure is needed. The existing image and Job pattern work: override the container args with
`["calm-down", "<fear>", "--out", "/data/runs/calm-down"]`, and collect the files from the runs volume.
Nothing here schedules runs or sends email.
