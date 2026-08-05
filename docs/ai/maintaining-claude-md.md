# Maintaining CLAUDE.md

`CLAUDE.md` at the repo root is the primary context file for AI coding agents. It only helps if it stays accurate. This doc explains how to keep it that way — it is guidance for humans and agents editing the file, not itself loaded as agent context.

## When to update it

Update `CLAUDE.md` in the same PR whenever you:

- Add, rename, or remove a command a developer would run (build, test, lint, deploy).
- Change a convention it documents (naming, error handling, folder layout).
- Add a new "don't touch without asking" area (new CI file, new generated code path, new migration system).
- Introduce a new major component that isn't obvious from the file tree.

## When *not* to update it

- Don't record information derivable from reading the code (exact function signatures, current file contents) — that goes stale and agents can just read the file.
- Don't log historical decisions here — use an ADR in [docs/adr/](../adr/) instead and link to it if relevant.
- Don't turn it into a tutorial. It's a reference for an agent that already knows how to code, not an onboarding doc for a new hire (that's [CONTRIBUTING.md](../../CONTRIBUTING.md)).

## Structure to preserve

Keep the existing section headers (First: check whether this repo is still an unadapted template, Project overview, Tech stack, Commands, Conventions, Testing expectations, Git & PR conventions, What not to touch, Architecture notes, Documentation, Before considering a task done) so the file stays predictable across projects that use this template. Add sub-sections under them rather than inventing new top-level ones, unless the project genuinely needs a new category.

The "First: check whether this repo is still an unadapted template" section should be deleted once the project is adapted (no more `{{TOKEN}}` placeholders anywhere) — it has no purpose after that point and just adds noise to every future session's context.

## Multiple CLAUDE.md files

For larger projects, nested `CLAUDE.md` files in subdirectories (e.g. `apps/api/CLAUDE.md`) can hold context specific to that area — agents pick up the nearest one plus the root. Only split it out once the root file is genuinely crowded; don't do it preemptively.

See [examples/nested-claude-md-example/CLAUDE.md](examples/nested-claude-md-example/CLAUDE.md) for a worked example of what a good nested file looks like — short, additive, and non-repeating of the root.
