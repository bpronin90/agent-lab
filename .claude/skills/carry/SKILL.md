---
name: carry
description: Use when the owner says "carry #N". Does everything "implement #N" does, then merges the PR and closes issue N once the merge conditions hold.
---

# carry #N

The owner's own `carry #N` message is START for issue #N and the go-ahead to merge
under the merge conditions in `CONTRIBUTING.md`.

1. Follow every step of the `implement` skill (`.claude/skills/implement/SKILL.md`),
   recording the owner's `carry #N` words as START.
2. Merge only when all hold for the exact PR head:
   - CI is green (`gh pr checks <PR> --watch`).
   - An independent, non-Claude `VERDICT=APPROVED` exists for that full SHA, with no
     unresolved blocking findings.
   - The PR does not deploy, release, migrate data, touch credentials, secrets, or
     repository settings, or cause external side effects.
   - The PR body contains `Closes #N`.
3. Re-confirm the head SHA, then `gh pr merge <PR> --squash --delete-branch`. Never
   `--admin`. If any condition fails, stop and report instead.
4. Confirm issue #N closed through the merge and report the merge commit SHA.
