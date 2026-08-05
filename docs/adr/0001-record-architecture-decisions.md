# ADR 0001: Record architecture decisions with ADRs

- Status: accepted
- Date: {{YYYY-MM-DD}}

## Context

Decisions about architecture and significant technical direction get made in PR discussions, Slack, or verbally, then forgotten. New contributors (human or AI) end up re-litigating settled questions or violating unstated constraints because the reasoning was never written down.

## Decision

We record architecturally significant decisions as Architecture Decision Records (ADRs) in `docs/adr/`, numbered sequentially, using the template at [template.md](template.md). A decision is "architecturally significant" if reversing it would be costly, or if it constrains future choices (e.g. choice of database, API style, auth approach, major dependency, module boundaries).

ADRs are immutable once accepted: to change a decision, write a new ADR that supersedes the old one and update the old one's status.

## Alternatives considered

- **Wiki page**: drifts out of sync with the repo, no PR review, no history tied to commits.
- **Comments in code**: good for local *why*, bad for cross-cutting decisions that don't live in one file.
- **No record at all**: status quo; the problem this ADR exists to fix.

## Consequences

Every significant decision has a discoverable, reviewed, dated record. Adds a small amount of process overhead: authors must remember to write one, and reviewers must remember to ask for one when a PR makes an undocumented architectural choice.
