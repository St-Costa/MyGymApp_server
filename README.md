<h1 align="center">MyGymApp Sync Server</h1>

<p align="center">
  <em>Self-hosted ingestion server for MyGymApp workout sessions, reachable only over Tailscale.</em><br>
</p>

<p align="center">
  <img alt="CI" src="https://github.com/St-Costa/MyGymApp_server/actions/workflows/ci.yml/badge.svg">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/St-Costa/MyGymApp_server"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white">
</p>

MyGymApp Sync Server receives workout data pushed by the MyGymApp Android app — workout
sessions, HRV readiness measurements, smart-scale weigh-ins, and raw ECG waveforms — stores
the raw bytes durably first, then parses them into a queryable SQLite layer as a second,
best-effort step — so a phone-side format change never risks silently losing data. See
[docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md),
[READINESS_SPEC.md](docs/sync-ingestion/READINESS_SPEC.md),
[SCALE_SPEC.md](docs/sync-ingestion/SCALE_SPEC.md), and
[ECG_SPEC.md](docs/sync-ingestion/ECG_SPEC.md) for the full design and rationale.

## Status

✅ All six ingestion endpoints (sessions, readiness, scale, ECG, repo files, repo bulk),
the restore endpoints, the git-versioned `data/raw/` backup store, storage layer, and
reparse tooling implemented per spec. Tailscale Serve setup (below) is a manual step to
run on the actual self-hosted box.

## API

| Endpoint | Record type | Spec |
|---|---|---|
| `POST /v1/sessions` | Workout session | [SPEC.md](docs/sync-ingestion/SPEC.md) |
| `POST /v1/readiness` | HRV readiness event | [READINESS_SPEC.md](docs/sync-ingestion/READINESS_SPEC.md) |
| `POST /v1/scale-weighins` | Smart-scale weigh-in | [SCALE_SPEC.md](docs/sync-ingestion/SCALE_SPEC.md) |
| `POST /v1/ecg` | Raw ECG waveform | [ECG_SPEC.md](docs/sync-ingestion/ECG_SPEC.md) |
| `POST /v1/repo` | Repo file (`exercises/*.md`, `routines/*.md`) — upsert/delete | [backup/README.md](docs/backup/README.md) |
| `POST /v1/repo/bulk` | N repo upserts/deletes in one request, one commit | [backup/README.md](docs/backup/README.md#batch-endpoints-optional) |
| `GET /v1/manifest` | Restore: content hash of every live raw file | [backup/README.md](docs/backup/README.md) |
| `GET /v1/file?relPath=…` | Restore: raw bytes of one live file | [backup/README.md](docs/backup/README.md) |
| `POST /v1/repo/files` | Restore: many live files in one `multipart/mixed` response | [backup/README.md](docs/backup/README.md#batch-endpoints-optional) |
| `GET /v1/repo/tarball` | Restore: gzip tar of every live file (`?since=` for a no-op when unchanged) | [backup/README.md](docs/backup/README.md#batch-endpoints-optional) |
| `GET /health` | — | no auth required |

The three text-record POST endpoints share the same request contract: `multipart/form-data`
with an `envelope` JSON part and a `file` Markdown part, `Authorization: Bearer
<shared-secret>`, and content-hash-based idempotency (same id + same hash → safe no-op
`duplicate`). `POST /v1/ecg` follows the same shape but its `file` part is gzip-compressed
binary (`application/gzip`), and its content hash covers the *compressed* bytes — see
ECG_SPEC.md for the deltas. Once the server accepts an ECG recording it is the phone's cue to
delete its own local copy — the server's raw store becomes the only copy that will ever
exist, so `raw/ecg/` needs the same (or better) backup discipline as `raw/`'s session files.

`GET /v1/analysis/*` (bearer auth, same as above) expose the ported analysis logic —
cardio trend/alerts, weekly resting-HR/VO2max/scale trends, exercise PRs — as JSON; see
[docs/sync-ingestion/ANALYSIS_SPEC.md](docs/sync-ingestion/ANALYSIS_SPEC.md).

**Cardio blocks**: the phone can mark explicit start/end cardio phases within a session
(`ExerciseType.CARDIO`) — no new endpoint, this rides the existing `POST /v1/sessions`
transport; `exercise_sets` gained four nullable columns (`started_at`/`ended_at`/`avg_hr`/
`max_hr`) for it. See [CARDIO_BLOCKS_SPEC.md](docs/sync-ingestion/CARDIO_BLOCKS_SPEC.md).

**Git-versioned backup**: `data/raw/` is a git repo — writes are committed in debounced
bursts (one background `git commit` ~3s after a burst settles), so any prior state or an
accidentally deleted file is recoverable. `POST /v1/repo` adds `exercises/*.md` /
`routines/*.md` as a fifth record type (upsert + soft-delete-to-`deleted/`), and
`GET /v1/manifest` + `GET /v1/file` are the pull-only restore path. Optional batch endpoints
(`POST /v1/repo/bulk`, `POST /v1/repo/files`, `GET /v1/repo/tarball`) collapse a
backfill/restore to one round-trip; the single-file endpoints stay as the fallback. Optional
off-box mirror via `BACKUP_GIT_REMOTE`. See [docs/backup/README.md](docs/backup/README.md)
and [docs/backup/OPTIMIZATION.md](docs/backup/OPTIMIZATION.md).

### ECG waveform analysis

[app/ecg_analysis.py](app/ecg_analysis.py) is a from-source port of the phone's
now-removed R-peak/HRV/arrhythmia analysis (Pan-Tompkins-style QRS detection, RMSSD/SDNN/
pNN50, Poincaré SD1/SD2/ratio, PAC/pause/irregular-beat counts, AFib suspicion screening) —
see [docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md](docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md)
for the full formulas and rationale. Results land in the `ecg_analysis` table, keyed by
`session_id`.

**Runs automatically**: `POST /v1/ecg` analyzes every recording synchronously right after
its header parses (see `app.ingestion.ingest_ecg`) — a bug or edge case in the analysis math
can never fail the upload itself, the recording just stays in the `analyzed_at IS NULL`
queue for a later retry. For the backlog (recordings that landed before this was wired in)
or bulk re-analysis after a formula change, run `analyze_ecg.py` — same never-touches-the-
network shape as `reparse.py`:

```bash
python analyze_ecg.py --all      # every recording never analyzed, ever
python analyze_ecg.py --stale    # only ECG_ANALYSIS_SCHEMA_VERSION < current
python analyze_ecg.py <session_id> [<session_id> ...]   # specific ids, forces re-run
```

The R-peak detector includes one deliberate extension beyond a literal port: a gap-filling
pass that recovers real beats missed when a sustained, wide motion artifact (electrode
contact during a heavy rep — lasting well past a QRS complex's ~150ms) inflates the trailing-
max threshold for several seconds. This was found by running the port against real gym-
session recordings, where it was inflating RMSSD/SDNN/AFib-suspicion counts session-wide.
See `app/ecg_analysis.py`'s module comment for the details.

**Advanced metrics** ([docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md](docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md)):
a second, literature-grounded layer on top of the port above — signal quality indices
(kurtosis SQI + beat-detection agreement against an independent second detector), DFA-α1
(fractal RR-interval scaling exponent, tracks the aerobic/ventilatory threshold in exercise
physiology), frequency-domain and nonlinear HRV (LF/HF, SampEn, CSI/CVI) via
[NeuroKit2](https://github.com/neuropsychology/NeuroKit), and an approximate heart rate
recovery (HRR1/HRR2). Also computed automatically as part of the same analysis pass — same
`ecg_analysis` table, same `analyze_ecg.py`/`analyzed_at` queue mechanics. See the doc for
what's *not* included from the same research pass (respiration-from-ECG, ensemble R-peak
detection, deep-learning arrhythmia classifiers) and why.

## Dashboard

A server-rendered dashboard is available at `/` (trends), `/exercises` (PR lookup),
`/exercises/{exercise_id}` (per-exercise detail), and `/settings/token` (shared-secret
rotation), same origin as the API, no separate service or build step. Deliberately **not**
behind the bearer token — this server is only ever reachable via Tailscale Serve on a
private tailnet, so there's no browser-side auth flow to build for a single-user tool (see
[app/gui.py](app/gui.py)'s module docstring for the reasoning). Just open
`https://<your-tailscale-hostname>/` once Tailscale Serve is set up (below).

## Getting started

### Prerequisites

- Python 3.12+
- [Tailscale](https://tailscale.com/) installed and joined to your tailnet, if running this
  for real (not required for local dev)

### Setup

```bash
make setup
# or, without make:
pip install -e ".[dev]"
cp .env.example .env   # fill in SYNC_SHARED_SECRET, DATA_ROOT, PORT
```

### Running

```bash
make dev
```

### Testing

```bash
make test
```

A [devcontainer](.devcontainer/devcontainer.json) is available for a reproducible environment (VS Code / GitHub Codespaces); tool versions are pinned in [.mise.toml](.mise.toml).

### Reparsing stored records

`reparse.py` re-runs the parser against raw files already on disk — no network access — and
is how the database catches up after a parser is improved to handle a new field. It covers
all six record types (sessions, readiness events, scale weigh-ins, ECG recordings,
exercises, routines) in one pass by default:

```bash
python reparse.py --all                    # reparse every stored record, all types
python reparse.py --stale                  # only schema_version < current, all types
python reparse.py --type readiness --all   # restrict to one record type
python reparse.py <id> [<id> ...]          # reparse specific record id(s)
```

## Running in production (Docker)

```bash
make build
docker run -d \
  --name mygymapp-sync-server \
  -p 127.0.0.1:8420:8420 \
  -v ~/mygymapp-server-data:/data \
  -e SYNC_SHARED_SECRET="$(openssl rand -hex 32)" \
  -e DATA_ROOT=/data \
  mygymapp-sync-server
```

Publishing to `127.0.0.1:8420` (not `0.0.0.0:8420` or a bare `-p 8420:8420`) is what keeps
this off the LAN/public internet — see the Tailscale Serve section below for how the phone
actually reaches it.

## Tailscale Serve setup

This server must be reachable **only** from your own tailnet (do **not** use Tailscale
Funnel — that exposes it to the public internet, which this design explicitly avoids). Once
the container above is running and bound to `127.0.0.1:8420`:

```bash
tailscale up                                            # if not already joined to the tailnet
tailscale serve --bg https / http://localhost:8420
tailscale serve status                                   # verify the mapping
```

This publishes the service at `https://<machine-name>.<tailnet-name>.ts.net` — visible via
`tailscale status` — over HTTPS with a MagicDNS-issued cert, reachable only by devices on
your tailnet (the phone running MyGymApp needs the Tailscale Android app, authenticated to
the same tailnet). **This hostname is what you put into the MyGymApp Android app's sync
settings**, together with the `SYNC_SHARED_SECRET` you generated above.

## Project structure

```
.
├── .claude/            # Claude Code settings (permissions)
├── .devcontainer/      # reproducible dev environment
├── .github/             # CI workflows, issue/PR templates, CODEOWNERS
├── docs/                 # project documentation
│   ├── adr/               # architecture decision records
│   ├── ai/                 # instructions and context for AI coding agents
│   ├── sync-ingestion/       # the implementation spec for this project
│   ├── DOCUMENTATION_GUIDE.md
│   └── RUNBOOK.md           # operational playbook (fill in once deployed)
├── app/                    # FastAPI application
│   ├── main.py               # app entrypoint, startup (DB init)
│   ├── routes.py              # API handlers (GET /health, /v1/analysis/*, POST /v1/sessions|readiness|scale-weighins|ecg|repo|repo/bulk, GET /v1/manifest|/v1/file|/v1/repo/tarball, POST /v1/repo/files)
│   ├── gui.py                  # server-rendered dashboard + token routes (no auth — see module docstring)
│   ├── templates/                # Jinja2 templates for the dashboard
│   ├── static/                    # vendored JS (Chart.js) served at /static
│   ├── analysis.py                 # ported trend/PR analysis logic (ANALYSIS_SPEC.md)
│   ├── ecg_analysis.py              # ECG R-peak/HRV/arrhythmia + advanced-metrics analysis
│   ├── ingestion.py                 # hash verification, raw write, idempotency (steps 3-7)
│   ├── parsing.py                    # YAML frontmatter parser, shared with reparse.py
│   ├── storage.py                     # SQLite schema + upsert logic
│   ├── repo.py                        # repo-file ingestion + restore (manifest/tarball)
│   ├── repo_worker.py                   # background SQL-view parse worker for repo files
│   ├── git_backup.py                     # debounced git committer for data/raw/
│   ├── weekly_report.py                   # deterministic weekly report content
│   ├── security.py                     # bearer-token auth + secret rotation
│   └── config.py                        # env-driven configuration (+ DATA_ROOT/.sync_secret override)
├── reparse.py               # standalone re-parse maintenance script (reuses app/parsing.py)
├── analyze_ecg.py           # standalone ECG-analysis queue-drain script (reuses app/ecg_analysis.py)
├── tests/                     # test suite
├── CLAUDE.md                # instructions for Claude Code and other AI agents
├── Makefile                  # stack-agnostic entry points (setup/dev/test/lint/build)
├── .mise.toml                 # pinned tool/runtime versions
├── lefthook.yml                 # git hooks: secret scanning, commit-msg linting
├── CONTRIBUTING.md
├── CHANGELOG.md
└── LICENSE
```

## Documentation

- [docs/API.md](docs/API.md) — endpoint contract reference (id fields, statuses, errors, examples)
- [docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md) — the implementation spec for this project
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to propose changes
- [docs/adr/](docs/adr/) — why key decisions were made
- [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) — how feature docs are organized
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operating this project once deployed
- [CLAUDE.md](CLAUDE.md) — how AI agents should work in this repo

## License

See [LICENSE](LICENSE).
