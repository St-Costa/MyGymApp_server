# CLAUDE.md

Instructions for Claude Code (and other AI coding agents that read this file) when working in this repository.

## Project overview

MyGymApp Sync Server is a standalone, self-hosted ingestion server for the MyGymApp Android
app. It receives one workout session (a Markdown file with YAML frontmatter) per `POST`
after each gym session, stores the raw bytes durably first, then parses them into a queryable
SQLite layer as a second, best-effort step. Single user, single shared secret, reachable only
over Tailscale (see [docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md) for the full
spec). "Done" for a typical change: the raw-first guarantee still holds (a parse failure never
loses or corrupts data), idempotency on `sessionId` + content hash still holds, and the
`reparse.py` script still shares its parsing logic with the live endpoint.

## Tech stack

- Language: Python 3.12
- Package manager: `pip` + `pyproject.toml` (via `mise`-pinned Python)
- Web framework: FastAPI (+ Uvicorn)
- Storage: SQLite (stdlib `sqlite3`), raw files on disk under `DATA_ROOT`
- Parsing: PyYAML for frontmatter
- Test framework: `pytest`
- Lint/format tooling: `ruff`
- Deploy target: Docker container on a self-hosted box, reachable only via `tailscale serve`

## Commands

<!-- Exact, copy-pasteable commands. Agents will run these verbatim. -->

| Task | Command |
|---|---|
| Install deps | `pip install -e ".[dev]"` |
| Run dev server | `uvicorn app.main:app --reload --port 8420` |
| Run tests | `pytest` |
| Run a single test | `pytest path/to/test_file.py::test_name` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Audit dependencies for CVEs | `pip-audit . --ignore-vuln PYSEC-2026-3447` (see ci.yml for why that one's excluded) |
| Build (Docker image) | `docker build -t mygymapp-sync-server .` |
| Reparse stored sessions | `python reparse.py --all` (or `--stale`, or specific IDs) |
| Re-run ECG analysis | `python analyze_ecg.py --all` (or `--stale`, or specific session IDs) |

## Conventions

- Follow the style already present in the file you're editing over any external style guide.
- Prefer editing existing files over creating new ones.
- No speculative abstractions: build what's asked, not what might be needed later.
- Keep functions/files close to the size and shape of neighboring code in the same directory.
- Comment only where the *why* isn't obvious from the code itself.
- **Raw-first, always**: never let a parsing failure prevent or corrupt the raw file write.
  The raw bytes on disk are the durable source of truth; the SQLite layer is a best-effort,
  re-derivable index over them. See [docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md)
  for the full reasoning.
- **One parser, two callers**: the YAML-frontmatter parsing logic used by `POST /v1/sessions`
  (step 6) must be the same function `reparse.py` calls — never duplicate it.
- Business logic (parsing, hashing, idempotency checks) lives outside the FastAPI route
  handlers so it's testable and reusable from `reparse.py` without spinning up the app.
- Bump `PARSER_SCHEMA_VERSION` by hand whenever the set of fields the parser extracts changes.

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

- Branch naming: `feature/short-description`, `fix/short-description`
- Keep PRs scoped to one logical change. Explain *why* in the PR description, not just *what*.
- Do not commit secrets, credentials, or `.env` files. See `.gitignore`.

## What not to touch without asking

- CI/CD workflow files (`.github/workflows/`) — changes here affect every future PR.
- Anything under `docs/adr/` — these are historical records, not living docs; add a new ADR instead of editing an old one.
- `.claude/settings.json`, `lefthook.yml`, `.gitleaks.toml`, `commitlint.config.js` — these govern what Claude itself is permitted to do and what gets checked before a commit lands; loosening them is a trust decision for the user to make, not the agent.
- The `sessions` / `exercise_sets` / `tonnage_by_bodypart` / `readiness_events` /
  `scale_weighins` / `ecg_recordings` / `exercises` / `routines` / `routine_exercises`
  SQLite schema — it's re-derivable from raw files via `reparse.py`, but a breaking change
  still needs a `PARSER_SCHEMA_VERSION` bump and a migration plan, not a silent edit.
- `data/raw/` is a git repository (`app/git_backup.py`, see [docs/backup/README.md](docs/backup/README.md)
  and [docs/backup/OPTIMIZATION.md](docs/backup/OPTIMIZATION.md)): commits are **debounced**
  (one background `git commit` per burst of writes, ~3s after the last), a 2xx means
  "raw bytes fsynced + hash verified" not "committed", and the `/v1/repo` YAML parse into
  the `exercises`/`routines` view runs on a worker thread (`app/repo_worker.py`). Don't
  change the debounce/flush model, the commit-message format, or the
  soft-delete-to-`deleted/` behaviour without a reason.
- `docs/sync-ingestion/SPEC.md` — the source spec for this project; treat as a historical
  record like an ADR. If behavior needs to diverge from it, document the divergence rather
  than editing the spec to match.

## Architecture notes

See [docs/sync-ingestion/PENDING_IMPROVEMENTS.md](docs/sync-ingestion/PENDING_IMPROVEMENTS.md)
for server-side calculation improvements identified but currently blocked on data the phone
doesn't sync yet (not code gaps) — check it before telling the user "we can't improve X yet"
without a reason, and update it when a blocked item becomes unblocked or a new one is found.

<!-- Point to docs/adr/ for detailed decisions. Use this section only for a short map of major components and how they talk to each other. -->

Module map: `routes.py` (thin HTTP adapters) → `ingestion.py` (hash, raw write,
idempotency) → `parsing.py` (bytes → dataclasses) + `storage.py` (SQLite upserts,
re-derivable via `reparse.py`); repo files go `routes.py` → `repo.py` (fsync +
manifest/tarball) → `repo_worker.py` (background SQL-view parse) + `git_backup.py`
(debounced committer, lifecycle in `main.py`); `gui.py` and `weekly_report.py` are
read-only consumers of `analysis.py` / `ecg_analysis.py`, never of the ingest path.

See [docs/adr/](docs/adr/) for the reasoning behind major architectural decisions.

## Documentation

When a change needs user-facing or developer-facing documentation (not just code comments), organize it by feature/domain area: `docs/<feature-name>/` rather than by document type. Update docs in the same commit/PR as the code change they describe — don't defer it. See [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) for the full structure and when a feature earns its own doc vs. a section in an existing one.

## Before considering a task done

Run through [docs/ai/review-checklist.md](docs/ai/review-checklist.md) — it consolidates the checks scattered across this file into one pass.

## Keeping this file useful

This file should describe the *current* state of the project, not its history. When you make a change that invalidates something above (a new command, a changed convention, a new "don't touch" area), update this file in the same PR. See [docs/ai/maintaining-claude-md.md](docs/ai/maintaining-claude-md.md) for guidance.
