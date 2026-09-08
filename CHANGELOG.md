# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- ECG ingestion (`POST /v1/ecg`) plus server-side R-peak/HRV/arrhythmia analysis, run
  automatically on upload with `analyze_ecg.py` as the backlog/`--stale` queue drain, plus
  the tier-1 advanced layer (SQI, DFA-alpha1, frequency-domain/nonlinear HRV, HRR).
- Git-versioned `data/raw/` backup store (debounced commits, off-box mirror) with repo-file
  ingestion (`POST /v1/repo`, `POST /v1/repo/bulk`) and pull-only restore (`GET /v1/manifest`,
  `GET /v1/file`, `POST /v1/repo/files`, `GET /v1/repo/tarball`).
- Server-rendered dashboard (`/`, `/exercises`, `/exercises/{id}`, `/weekly-report` preview)
  with cardio/ACWR/RPE/monotony/HR-zone/muscle-group/maintenance charts, and the
  deterministic weekly report content generation.
- CARDIO exercise blocks, session-RPE/Foster load, bodyweight-load audit fields, STRETCH
  held seconds, daily step average, readiness `sleepQuality`, server-side VO2max/calorie/
  TRIMP recomputes.
- `AGENTS.md` pointer and `docs/API.md` endpoint reference for AI coding agents.

### Changed

- Single-query weekly trend series, lazy scientific-stack imports, WAL SQLite mode,
  parent-dir fsyncs, and streaming batch reads — same wire contracts, lower latency/RAM.

### Fixed

- `GET /health` returns `{"ok": true}`; install `git` in the Docker image; reap git zombie
  processes (`init: true`); stale "not implemented" spec headers corrected.

### Removed

- Weekly-report screenshot generation and scheduler (reverted experiment).
