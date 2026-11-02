# Calm Down: fixture preview

**FIXTURE PREVIEW. Not live research. Do not send.**

Generated offline with `agent-lab calm-down "AI will replace lawyers and accountants" --out examples/calm-down` followed by
`agent-lab calm-down-finalize examples/calm-down --review fixture` using the mock
provider and the hand-written corpus in `src/agent_lab/agents/calm_down/fixtures/`. The source excerpts
paraphrase real public works from memory. They are not verified quotations, so check every number at the
linked source. The claims were approved by the authoring agent's own fixture self-review, which is not independent. The run shows the research → verification → rendering pipeline, not the accuracy of the content.

- `email.html`: email-safe HTML (inline CSS, no JS, no remote images)
- `email.txt`: plain-text version
- `research-dossier.md`: evidence by kind, opposing findings, uncertainty, provenance
- `bundle.json`: the immutable reviewed draft (dossier plus source-text snapshot) that finalize renders
- `review-request.json`: what a human reviewer approves (claims, quotes, confidence, source digests, presentation)
- `sources.json`: retrieval provenance (backend, retrieval time, SHA-256 of the text)
