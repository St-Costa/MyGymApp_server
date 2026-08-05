# Using this template

Checklist for turning this template into a real project. Delete this file once done.

Claude Code runs this automatically: [CLAUDE.md](../CLAUDE.md) instructs it to detect leftover `{{PLACEHOLDER}}` tokens at the start of a session and offer to work through this checklist before starting on anything else. You can still run it by hand, or just tell Claude your stack and project details and let it drive.

## 1. Replace placeholders

Search the repo for these tokens and replace them:

| Token | Replace with |
|---|---|
| `{{PROJECT_NAME}}` | Project name |
| `{{PROJECT_DESCRIPTION}}` | One-paragraph description (in `CLAUDE.md`) |
| `{{OWNER}}` | GitHub org/user |
| `{{REPO}}` | Repo name |
| `{{AUTHOR}}` | Copyright holder (in `LICENSE`) |
| `{{YEAR}}` | Current year (in `LICENSE`) |
| `{{SECURITY_CONTACT_EMAIL}}` | Where security reports should go (in `SECURITY.md`) |

```bash
# example: find every placeholder token left to fill in
grep -rn '{{[A-Z_]*}}' --include='*.md' --include='*.yml' .
```

## 2. Pick your stack

This template intentionally ships no language-specific tooling. For your stack:

- Add real dependency/build files (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, etc.)
- Replace the placeholder job in [.github/workflows/ci.yml](../.github/workflows/ci.yml) with real install/lint/test/build steps.
- Fill in the `Commands` table in [CLAUDE.md](../CLAUDE.md) and the `Getting started` section in [README.md](../README.md).
- Fill in the README header block: project description, the intro paragraph below the badges, and add any stack-specific badges (language, framework, etc.) alongside the CI/License ones.
- Uncomment the relevant section of [.gitignore](../.gitignore).
- Pin real tool versions in [.mise.toml](../.mise.toml) (or delete it if you don't want version pinning).
- Wire up the placeholder targets in [Makefile](../Makefile) to your real setup/test/lint/build commands — keep them in sync with the `Commands` table in `CLAUDE.md`.
- Set the base image and `postCreateCommand` in [.devcontainer/devcontainer.json](../.devcontainer/devcontainer.json), or delete `.devcontainer/` if you don't want a containerized dev environment.
- Uncomment and wire up the `lint`/`format` hooks in [lefthook.yml](../lefthook.yml), then run `lefthook install` once per clone to activate git hooks (gitleaks secret-scanning and commitlint are active by default; both need to be installed locally — see their own docs).

## 3. Fill in CLAUDE.md

This is the single most valuable file for AI-assisted development. At minimum, fill in:

- Project overview
- Tech stack
- Commands table
- Git & PR conventions

See [docs/ai/maintaining-claude-md.md](ai/maintaining-claude-md.md) for how to keep it accurate over time.

Note: `CLAUDE.md` instructs Claude to commit locally on its own after each logically complete change, without waiting to be asked (it still won't push, force-push, or touch CI config without confirmation). If you'd rather review changes before they're committed, edit the "Commit automatically, without being asked" section in `CLAUDE.md` before starting work.

## 4. Decide on CODEOWNERS

Edit [.github/CODEOWNERS](../.github/CODEOWNERS) or delete it if not needed yet.

## 5. Clean up

- Delete this file.
- Delete the "First: check whether this repo is still an unadapted template" section at the top of [CLAUDE.md](../CLAUDE.md) — once no `{{TOKEN}}` placeholders remain it has nothing left to detect.
- Delete `src/.gitkeep` and `tests/.gitkeep` once real files exist there.
- Update the README badge URLs once the repo exists on GitHub with CI configured.
- If you don't need ADRs, delete `docs/adr/` — but consider keeping [ADR 0001](adr/0001-record-architecture-decisions.md) since it explains why the practice exists.
- Fill in [docs/RUNBOOK.md](RUNBOOK.md) once something is actually deployed — it's placeholder-only until then. Delete it if this project will never be operated as a live service (e.g. a library).
- Delete `docs/ai/examples/` — it's a worked example, not something meant to ship in a real project.

## 6. First commit

Reset history so the new project doesn't inherit template commits (optional, but common):

```bash
rm -rf .git
git init
git add .
git commit -m "chore: initialize project from template"
```
