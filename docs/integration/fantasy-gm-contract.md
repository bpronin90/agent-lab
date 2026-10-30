# Fantasy GM integration contract (v1.0)

Status: **defined, not wired**. `bpronin90/fantasy-gm` has not been modified.

## Boundary
```
Fantasy GM (producer)                      Agent Lab (consumer)
Research Pack output ─► ResearchPackEnvelope ─► fantasy agent (Phase 2) ─► AdvisoryDecision
                                                                     └► agent_lab.decisions (human_status=pending)
```
- Source of truth: `src/agent_lab/contracts/fantasy_gm.py`.
- Language-neutral copies: `contracts/research_pack_envelope.schema.json` and `contracts/advisory_decision.schema.json`.
  CI fails if they drift from the Python source.
- Example: `tests/fixtures/fantasy_gm_envelope.sample.json` (synthetic, `is_sample: true`).

## Rules, which mirror Fantasy GM's AGENTS.md
1. **Advisory only.** `AdvisoryDecision.advisory_only` is the literal `true`. No roster transactions, ever.
2. **No credentials or private data cross the boundary.** Unknown fields are rejected (`extra="forbid"`),
   so a stray token field fails validation. Real rosters stay out of Git.
3. **Sample awareness.** `is_sample` flows through to `based_on_sample_data`.
4. **Verification.** Each recommendation lists `requires_verification`: injuries, eligibility, and scoring when
   `scoring_verified` is false. External text is untrusted.
5. **Field conventions match Fantasy GM.** Uppercase NFL abbreviations, `DEF`, the slot names
   (`FLEX`, `SUPER_FLEX`, `BN`, `IR`, `IR+`), and `matchup` as `null` when unknown or `BYE`.
6. **Versioning.** Additive optional fields → `1.x`. Anything breaking → `2.0`, with both versions accepted during migration.

## Suggested producer change (Phase 2, needs your approval in that repo)
Add a small exporter to the Fantasy GM repository that maps the Research Pack's
roster, waivers and slot gaps to this envelope and writes JSON to its ignored `data/private/`.
Agent Lab would read that file, or receive it through a manual `workflow_dispatch` input.
No shared database access is required.
