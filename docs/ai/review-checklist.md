# Self-review checklist

Before considering a task done, run through this. It consolidates checks that are otherwise scattered across `CLAUDE.md` — use it as a final pass, not a replacement for reading the fuller sections it links to.

## Code

- [ ] Solves what was asked — no unrelated refactors, no speculative abstractions ([CLAUDE.md § Conventions](../../CLAUDE.md#conventions))
- [ ] Matches the style of surrounding code, not an external style guide
- [ ] No leftover debug prints, commented-out code, or TODOs without an issue reference

## Tests

- [ ] New behavior has a test; bug fixes have a regression test ([CLAUDE.md § Testing philosophy](../../CLAUDE.md#testing-philosophy))
- [ ] Full test suite run, not just the new/changed test
- [ ] No test weakened, skipped, or deleted just to make it pass

## Quality gates

- [ ] Lint, format, and type-check commands from `CLAUDE.md` run clean
- [ ] Pre-commit hooks (if configured, see [.pre-commit-config.yaml](../../.pre-commit-config.yaml) / [lefthook.yml](../../lefthook.yml)) pass

## Documentation

- [ ] `CLAUDE.md` updated if this change adds/changes a command, convention, or "don't touch" area
- [ ] Feature docs under `docs/<feature>/` updated if user-facing or developer-facing behavior changed ([docs/DOCUMENTATION_GUIDE.md](../DOCUMENTATION_GUIDE.md))
- [ ] New architectural decision recorded as an ADR if applicable ([docs/adr/](../adr/))

## Git

- [ ] Change is logically complete and working — not a partial step ([CLAUDE.md § Commit automatically](../../CLAUDE.md#commit-automatically-without-being-asked))
- [ ] Staged changes reviewed (`git status`/`git diff`) — no unrelated files, no secrets
- [ ] Commit message follows the project's format

## Security

- [ ] No secrets, credentials, or `.env` values in the diff
- [ ] Input from users/external systems validated at the boundary, not trusted implicitly
