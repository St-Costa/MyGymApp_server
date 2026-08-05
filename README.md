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

MyGymApp Sync Server receives workout session files pushed by the MyGymApp Android app after
every gym session, stores the raw bytes durably first, then parses them into a queryable
SQLite layer as a second, best-effort step — so a phone-side format change never risks
silently losing data. See [docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md) for the
full design and rationale.

## Status

✅ Ingestion endpoint, storage layer, and reparse tooling implemented per the spec. Tailscale
Serve setup (below) is a manual step to run on the actual self-hosted box.

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

### Reparsing stored sessions

`reparse.py` re-runs the parser against raw files already on disk — no network access — and
is how the database catches up after the parser is improved to handle a new field:

```bash
python reparse.py --all           # reparse every stored session
python reparse.py --stale         # reparse only sessions below the current schema_version
python reparse.py <id> [<id> ...] # reparse specific session ids
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
│   ├── routes.py              # HTTP handlers (GET /health, POST /v1/sessions)
│   ├── ingestion.py            # hash verification, raw write, idempotency (steps 3-7)
│   ├── parsing.py               # YAML frontmatter parser, shared with reparse.py
│   ├── storage.py                # SQLite schema + upsert logic
│   ├── security.py                # bearer-token auth
│   └── config.py                   # env-driven configuration
├── reparse.py               # standalone re-parse maintenance script (reuses app/parsing.py)
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

- [docs/sync-ingestion/SPEC.md](docs/sync-ingestion/SPEC.md) — the implementation spec for this project
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to propose changes
- [docs/adr/](docs/adr/) — why key decisions were made
- [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) — how feature docs are organized
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operating this project once deployed
- [CLAUDE.md](CLAUDE.md) — how AI agents should work in this repo

## License

See [LICENSE](LICENSE).
