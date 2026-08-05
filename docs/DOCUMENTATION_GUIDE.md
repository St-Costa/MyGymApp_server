# Documentation guide

How documentation in this repo is organized. This governs `docs/` content about the *product/system* — not the meta-docs about process (`CONTRIBUTING.md`, `docs/ai/`) or historical decisions (`docs/adr/`), which have their own fixed homes.

## Structure: by feature/domain, not by doc type

Each significant feature or domain concept gets its own folder:

```
docs/
├── adr/                  # architecture decision records (fixed, see docs/adr/)
├── ai/                    # AI agent process docs (fixed)
├── <feature-a>/
│   └── README.md          # what it is, how it works, how to use/extend it
├── <feature-b>/
│   ├── README.md
│   └── <sub-topic>.md     # split out only once README.md gets unwieldy
└── DOCUMENTATION_GUIDE.md # this file
```

Rationale: as a project grows, "where do I document X" should map to "where does X live conceptually," which is how both users and agents already navigate the codebase. A type-based split (guides/reference/concepts) forces every feature's story across three folders and tends to rot faster because updating a feature means remembering to touch three places instead of one.

## What goes in a feature doc

A feature doc (`docs/<feature-name>/README.md`) should cover, roughly in this order:

1. **What it does** — one or two sentences, user-facing framing.
2. **How to use it** — the common path, with real examples/commands.
3. **How it works** — enough of the internal model that a future contributor (or agent) can extend it safely, without duplicating the code itself.
4. **Edge cases / gotchas** — anything non-obvious that isn't a bug but has bitten someone before.
5. **Related** — links to relevant ADRs, other feature docs it depends on or is depended on by.

Keep it current: if a PR changes behavior a doc describes, update the doc in the same PR. A stale doc is worse than no doc.

## When a feature earns its own folder

- It has a distinct, nameable identity a user or contributor would search for ("auth", "billing", "webhooks") — not every function or module needs one.
- It's non-trivial enough that "read the code" isn't a reasonable substitute for a newcomer.
- Small, closely related features can share one folder/doc rather than each getting their own — don't fragment prematurely. Split a shared doc into per-topic files only once it gets long enough that finding things in it is hard.

## What doesn't go here

- Process/contribution instructions → `CONTRIBUTING.md`
- AI agent instructions and conventions → `CLAUDE.md`, `docs/ai/`
- Why a technical decision was made → `docs/adr/`
- API reference generated from code (OpenAPI, docstrings, etc.) → generate it; don't hand-write a duplicate here. Link to the generated output from the relevant feature doc instead.
