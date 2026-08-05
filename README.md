<h1 align="center">{{PROJECT_NAME}}</h1>

<p align="center">
  <em>One-sentence description of what this project does and who it's for.</em><br>
</p>

<p align="center">
  <img alt="CI" src="https://github.com/{{OWNER}}/{{REPO}}/actions/workflows/ci.yml/badge.svg">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/{{OWNER}}/{{REPO}}"></a>
  <!-- Add stack-specific badges here, e.g.:
  <img alt="Node" src="https://img.shields.io/badge/Node-20-339933?logo=node.js&logoColor=white">
  -->
</p>

{{PROJECT_NAME}} is <!-- one or two sentences: what it's based on/for, and the core design/technical choice that defines it -->.

## Status

🚧 Template — replace this section once the project has a real status (active, beta, maintenance, archived).

## Getting started

### Prerequisites

- List runtime/toolchain versions here (e.g. Node 20+, Python 3.12+, Go 1.22+).

### Setup

```bash
make setup
# or, without make: clone, install dependencies per this project's stack
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
│   ├── DOCUMENTATION_GUIDE.md
│   └── RUNBOOK.md           # operational playbook (fill in once deployed)
├── src/                    # application/library source (create per-language)
├── tests/                   # test suite (create per-language)
├── CLAUDE.md                # instructions for Claude Code and other AI agents
├── Makefile                  # stack-agnostic entry points (setup/dev/test/lint/build)
├── .mise.toml                 # pinned tool/runtime versions
├── lefthook.yml                 # git hooks: secret scanning, commit-msg linting
├── CONTRIBUTING.md
├── CHANGELOG.md
└── LICENSE
```

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) — how to propose changes
- [docs/adr/](docs/adr/) — why key decisions were made
- [docs/DOCUMENTATION_GUIDE.md](docs/DOCUMENTATION_GUIDE.md) — how feature docs are organized
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operating this project once deployed
- [CLAUDE.md](CLAUDE.md) — how AI agents should work in this repo

## License

See [LICENSE](LICENSE).
