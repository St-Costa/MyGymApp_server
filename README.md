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

🚧 Early implementation — ingestion endpoint, storage layer, and Tailscale setup are being
built out per the spec.

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
├── app/                    # FastAPI application (routes, parsing, storage)
├── reparse.py               # standalone re-parse maintenance script
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
