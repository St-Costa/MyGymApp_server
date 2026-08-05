# CLAUDE.md — example nested file (not a real subdirectory)

This file exists purely as a worked example of what a nested `CLAUDE.md` looks like once a project grows large enough to need one (see [maintaining-claude-md.md § Multiple CLAUDE.md files](../../maintaining-claude-md.md#multiple-claude-md-files)). It is not wired into any real subdirectory — copy the shape, not the content, into e.g. `apps/api/CLAUDE.md` or `packages/worker/CLAUDE.md` when the time comes.

A nested file should be short and additive: it assumes the agent has already read the root `CLAUDE.md` and only states what's *different* here.

---

Example content for a hypothetical `apps/api/CLAUDE.md`:

```markdown
# CLAUDE.md — apps/api

Extends the root CLAUDE.md. Read that first. This file covers only what differs inside `apps/api/`.

## Scope

This is the HTTP API service. It does not include the worker (`apps/worker/`) or the shared library (`packages/core/`) — see their own CLAUDE.md files if you're touching those.

## Commands (override root table for this directory)

| Task | Command |
|---|---|
| Run tests (this app only) | `pnpm --filter api test` |
| Run dev server | `pnpm --filter api dev` |

## Conventions specific to this app

- Route handlers live in `src/routes/`, one file per resource; business logic goes in `src/services/`, never in the handler.
- All new endpoints need an OpenAPI annotation — see `src/routes/users.ts` for the pattern.

## Don't touch without asking

- `src/middleware/auth.ts` — shared by every route; a bug here is a security incident, not just a regression.
```

---

Notes on the pattern:

- Don't repeat anything the root file already says — link back to it instead of restating.
- Only the sections that differ need to appear; a nested file with nothing to add shouldn't exist.
- Keep it in the directory it governs, named exactly `CLAUDE.md`, so agents discover it automatically when working there.
