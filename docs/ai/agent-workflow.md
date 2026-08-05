# Working with AI coding agents in this repo

Practical guidance for how agents (Claude Code or otherwise) should operate here, beyond what's in the root [CLAUDE.md](../../CLAUDE.md).

## Before starting a task

1. Read `CLAUDE.md` for commands and conventions.
2. Check [docs/adr/](../adr/) if the task touches architecture — don't relitigate a decision that's already recorded there without flagging it explicitly.
3. For anything non-trivial (new dependency, schema change, new external service, cross-cutting refactor), propose a short plan before writing code.

## While working

- Run the project's real test suite and lint/type-check commands from `CLAUDE.md` before calling a task done — don't just eyeball the diff.
- Keep changes scoped to the task. If you notice an unrelated issue, mention it rather than fixing it inline.
- If a command in `CLAUDE.md` is wrong or missing, fix the doc as part of your change.

## Opening a PR

- Follow the commit/branch conventions in `CLAUDE.md`.
- PR description: what changed and *why*. Link the ADR if the change follows one, or propose a new one if it's an architectural change without a prior record.
- Note any manual testing performed, especially for UI changes that can't be fully verified by automated tests.

## Escalation

If a task requires a decision only a human can make (ambiguous requirements, a tradeoff with real user-facing consequences, anything destructive/irreversible), stop and ask rather than guessing.
