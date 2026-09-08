# ADR 0002: Dashboard without bearer auth

- Status: accepted
- Date: 2026-08-15

## Context

The server-rendered dashboard (`app/gui.py`: `/`, `/exercises`, `/weekly-report`) lives on
the same origin as the API, whose `/v1/*` endpoints require the shared-secret bearer
token. The question was whether the dashboard needs its own browser-side auth flow.

## Decision

No auth on the dashboard. The server is only ever reachable via Tailscale Serve on a
private tailnet (never Funnel, never LAN/public), so the tailnet membership *is* the
access control — there is no meaningful attacker model left for a token flow to defend
against in a single-user tool, and building one would be complexity with no real security
benefit.

## Alternatives considered

- **Bearer token in the browser**: would need token storage/refresh UX for exactly one
  user, with no additional protection beyond what the tailnet already provides.
- **Separate auth service / login page**: same objection, heavier.

## Consequences

Opening the dashboard is one URL with no login. If the reachability assumption ever
changes (anything beyond `127.0.0.1` host publishing + Tailscale Serve), this decision
must be revisited — see the module docstring in `app/gui.py`, which states the reasoning
at the code site.
