# CLAUDE.md

Instructions for Claude Code (and other AI coding agents that read this file) when working in this repository.

## First: check whether this repo is still an unadapted template

Before doing anything else in a new session, check whether this project has been adapted from the template yet: search for unreplaced `{{TOKEN}}` placeholders (`grep -rn '{{[A-Z_]*}}'` across the repo, excluding `.git/`). If any remain — including the one in this file's "Project overview" section below — the repo is still in raw template state.

In that case, **don't just start on the user's request on top of a half-configured repo**. Proactively say so, then offer to run the adaptation now: ask what language/stack, project name, and any other details from [docs/USING_THIS_TEMPLATE.md](docs/USING_THIS_TEMPLATE.md) are needed, and work through that checklist (replace placeholders, wire up `Makefile`/CI/`.mise.toml`/`lefthook.yml` for the chosen stack, fill in this file's own Tech stack and Commands sections). Once no placeholders remain, proceed with what the user actually asked for.

If the user explicitly says to skip adaptation or work around the placeholders as-is, respect that instead.

## Project overview

<!-- Replace with a short, concrete description: what this project is, who uses it, and what "done" looks like for a typical change. -->

{{PROJECT_DESCRIPTION}}

## Tech stack

<!-- List language(s), frameworks, package manager, and any non-obvious infra (database, queue, deploy target). Keep this current — it's the first thing an agent reads. -->

- Language:
- Package manager:
- Test framework:
- Lint/format tooling:
- Deploy target:

## Commands

<!-- Exact, copy-pasteable commands. Agents will run these verbatim. -->

| Task | Command |
|---|---|
| Install deps | `` |
| Run dev server | `` |
| Run tests | `` |
| Run a single test | `` |
| Lint | `` |
| Format | `` |
| Build | `` |
| Type-check | `` |

## Conventions

- Follow the style already present in the file you're editing over any external style guide.
- Prefer editing existing files over creating new ones.
- No speculative abstractions: build what's asked, not what might be needed later.
- Keep functions/files close to the size and shape of neighboring code in the same directory.
- Comment only where the *why* isn't obvious from the code itself.

<!-- Add project-specific conventions: naming, error handling patterns, where business logic lives vs. handlers, etc. -->

## Testing expectations

- New behavior needs a test. Bug fixes need a regression test.
- Run the full test suite before considering a task done; do not just run the one test you wrote.
- Do not weaken or delete a test to make it pass.

### Testing philosophy: coverage by risk, not by percentage

Write as many tests as are *useful* — which is not the same as as many as possible. Apply the same judgment here you'd apply to production code: more tests are only better when each one earns its place.

**Prioritize tests for:**

- Business logic and domain rules — anything encoding "what the product is supposed to do."
- Edge cases: empty inputs, boundary values, concurrent access, malformed data from external sources.
- Every bug fix gets a regression test that fails without the fix and passes with it.
- Code with non-obvious behavior — anything a future reader (human or agent) could plausibly get wrong when modifying it.
- Integration points: anywhere this codebase talks to a database, external API, queue, or filesystem, since these are where real bugs hide and unit tests alone won't catch them.

**Deprioritize or skip tests for:**

- Trivial getters/setters, simple pass-through functions, and framework/library code you don't own.
- Generated code.
- Restating the implementation in test form (a test that just mirrors the function body line-for-line catches nothing).

**Anti-goals — these count against you, not for you:**

- Padding a coverage number with low-value tests (e.g. asserting a constructor doesn't throw) instead of covering actual risk.
- One giant test asserting many unrelated things — prefer focused tests with one clear failure reason each.
- Tests so tightly coupled to implementation details that a harmless refactor breaks them. Test behavior/contracts, not internals.
- Flaky tests left in place "for coverage." A flaky test is worse than no test — fix it or delete it.

When in doubt about whether a test is worth writing, ask: *if this broke, would an existing test catch it, and would that failure be useful signal?* If not, write it. If a test wouldn't tell you anything you don't already know, skip it.

## Git & PR conventions

### Commit automatically, without being asked

In this repository, committing is part of finishing a unit of work — you do not need explicit permission to run `git commit`, and you should not wait to be told. This overrides the general default of only committing when asked.

- **When to commit**: after each logically complete, working change — a bug fixed, a feature/sub-feature working, a refactor landed, docs updated to match a code change. Don't commit broken or half-finished work; don't bundle unrelated changes into one commit; don't wait for an entire multi-step task to finish if it contains several independent complete steps along the way.
- **What to do instead of asking first**: commit, then tell the user what you committed and why, in one line (e.g. "Committed as `abc1234`: fix null check in parser"). Report after, don't ask before.
- **Scope stays local**: this permission covers local commits only. Never push, force-push, rebase published history, or amend an existing commit without being explicitly asked — those remain confirm-first actions per the repo's normal safety rules.
- **Staging discipline still applies**: review what's staged before committing (`git status`/`git diff`), stage specific files rather than `git add -A`/`.` when unrelated changes are present, and check for secrets before committing anything that looks like it might contain them.
- **Exceptions** — do not auto-commit:
  - Mid-edit or exploratory work that doesn't yet build/pass tests.
  - Changes to `.github/workflows/`, other CI/CD config, or anything else listed under "What not to touch without asking" below — confirm first even for a commit.
  - If the user has asked you to hold changes uncommitted (e.g. for their own review before it enters history).

### Commit message format

<!-- Default below is Conventional Commits; replace with the project's actual convention if different. -->

```
<type>(<scope>): <summary, imperative mood, no trailing period>

<body — optional, wrap ~72 cols, explain *why* not *what*>
```

- `type`: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `style`, `build`, `ci`
- `scope`: optional, the affected area (module/package/feature name)
- Summary: imperative ("add", not "added"/"adds"), under ~72 chars, describes the change from the reader's perspective, not the process ("fix off-by-one in pagination", not "fixed bug")
- Body: only when the summary alone doesn't explain *why* — link an issue/ADR if relevant. Skip it for small, self-explanatory changes.
- One logical change per commit. If you're using "and" to describe the diff, it's probably two commits.

### Branching and PRs

- Branch naming: <!-- e.g. `feature/short-description`, `fix/short-description` -->
- Keep PRs scoped to one logical change. Explain *why* in the PR description, not just *what*.
- Do not commit secrets, credentials, or `.env` files. See `.gitignore`.

## What not to touch without asking

<!-- List anything sensitive: migrations, CI/CD config, infra-as-code, generated files, vendored code. -->

- CI/CD workflow files (`.github/workflows/`) — changes here affect every future PR.
- Anything under `docs/adr/` — these are historical records, not living docs; add a new ADR instead of editing an old one.
- `.claude/settings.json`, `lefthook.yml`, `.gitleaks.toml`, `commitlint.config.js` — these govern what Claude itself is permitted to do and what gets checked before a commit lands; loosening them is a trust decision for the user to make, not the agent.

## Architecture notes

<!-- Point to docs/adr/ for detailed decisions. Use this section only for a short map of major components and how they talk to each other. -->

See [docs/adr/](docs/adr/) for the reasoning behind major architectural decisions.

## Documentation

When a change needs user-facing or developer-facing documentation (not just code comments), organize it by feature/domain area: `docs/<feature-name>/` rather than by document type. Update docs in the same commit/PR as the code change they describe — don't defer it. See [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) for the full structure and when a feature earns its own doc vs. a section in an existing one.

## Before considering a task done

Run through [docs/ai/review-checklist.md](docs/ai/review-checklist.md) — it consolidates the checks scattered across this file into one pass.

## Keeping this file useful

This file should describe the *current* state of the project, not its history. When you make a change that invalidates something above (a new command, a changed convention, a new "don't touch" area), update this file in the same PR. See [docs/ai/maintaining-claude-md.md](docs/ai/maintaining-claude-md.md) for guidance.
