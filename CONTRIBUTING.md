# Contributing

Thanks for considering a contribution. This doc covers the process; see [CLAUDE.md](CLAUDE.md) for exact commands and conventions (also useful for humans, not just AI agents).

## Before you start

- For anything beyond a small fix, open an issue first to discuss the approach.
- Check [docs/adr/](docs/adr/) if your change touches architecture — it may already be discussed there.

## Development setup

See [README.md](README.md#getting-started).

## Making a change

1. Create a branch: `git checkout -b feature/short-description` (or `fix/...`).
2. Make your change, following the conventions in [CLAUDE.md](CLAUDE.md).
3. Add or update tests.
4. Run the full test suite and lint/format/type-check commands (see [CLAUDE.md](CLAUDE.md#commands)) before opening a PR.
5. Update `CLAUDE.md` and/or `README.md` if you changed a command, convention, or the project structure.

## Commit messages

<!-- State the convention, e.g. Conventional Commits -->

## Opening a pull request

- Fill in the PR template.
- Keep PRs focused on one logical change.
- Link the related issue.
- Ensure CI passes before requesting review.

## Code review

- Reviewers: focus on correctness, security, and maintainability, not style nitpicks the linter should catch.
- Authors: respond to every comment, either with a change or a reason you're not making it.

## Reporting bugs / requesting features

Use the issue templates. See [SECURITY.md](SECURITY.md) instead for security vulnerabilities — do not open a public issue for those.
