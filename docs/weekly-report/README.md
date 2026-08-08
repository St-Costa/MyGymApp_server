# Weekly report — content generation

Deterministic (no LLM) weekly summary of the most recently completed
Monday-Sunday week, built from data already computed in
[`app/analysis.py`](../../app/analysis.py). Implemented in
[`app/weekly_report.py`](../../app/weekly_report.py) as `build_weekly_report(conn,
week_end=...)`.

This document covers **content generation only** — the resulting
`WeeklyReport.body`/`title`/`priority`/`tags` are shaped for ntfy, but nothing
here sends anything. The ntfy client and weekly scheduler are a future
addition, expected to extend this same doc with "how it's sent" rather than
get a separate one.

## Reported week

The most recently completed Monday-Sunday week as of the day the job runs —
if it runs any day this week, it reports the week before, never a week still
in progress. Metrics that already use a longer native window in
`app.analysis` (ACWR's 7/28-day rolling windows, the cardio trend card's
28-day window) keep that window; only the exact-week numbers (session count,
weekly tonnage, weight/VO2max for *this* week) are strictly Monday-Sunday.

## Sections, in order

1. **Weight/BMI/BF%/lean mass** — exact numbers, always included when a
   weigh-in exists that week. This and VO2max are the two areas the user
   explicitly wants reported in full, so they're never silenced for being
   "normal" — only for being genuinely absent (which itself gets an explicit
   "no weigh-in this week" line, not a blank).
2. **VO2max** — exact number (median of the week's sessions), same
   always-included rule as weight.
3. **Training load (ACWR tonnage + RPE-ACWR)** — status only (semaphore +
   direction arrow), never the raw ratio. Included only when notable: either
   semaphore is not GREEN, or the week-over-week direction isn't flat.
   Silenced (nothing printed) when both ACWR variants are GREEN and stable.
4. **Cardio/HRV + volume/PRs** — session count and total tonnage are always
   shown, even at zero (an empty week is a signal worth stating, not noise to
   hide). Cardio metrics (resting HR, HRR60s, RMSSD, SDNN, drift, Poincaré
   ratio) get a status line only when YELLOW/RED or moving (not flat) — GRAY
   (no data at all for that metric, e.g. no ECG ever recorded) is treated as
   quiet, not notable, so a metric the user simply never populates doesn't
   permanently keep this section noisy. New PRs (a week's best single-set
   tonnage for an exercise equalling its all-time PR) are always listed when
   present. Self-diagnosis alerts from `compute_cardio_trend_card` (AFib
   suspicion, cardiac pauses, elevated resting HR vs baseline, chronically
   low HRR, insufficient data) are always appended verbatim.

If both synthetic-status sections (training load, cardio) have nothing
notable to say, a single "Tutto in norma questa settimana" line replaces them
— the report never goes silent about a status area, it either says something
specific or says explicitly that nothing is.

## Missing data

Each of the three underlying data sources (sessions, readiness events, scale
weigh-ins) is independent and can legitimately be absent for a given week —
this is what raw-first ingestion implies. No section raises an exception on
missing data; each either prints an explicit "no data this week" line (weight,
VO2max) or reflects the absence in its own numbers (zero sessions, zero
tonnage — stated plainly, never silenced, since an empty week is itself worth
knowing about, not indistinguishable from "everything's fine").

## Priority/tags (ntfy-shaped, not yet wired to a client)

- `priority` (1-5): 5 whenever any self-diagnosis alert is present; otherwise
  driven by the worst semaphore among included sections (RED→5, YELLOW→4,
  otherwise 3).
- `tags`: always includes `bar_chart`; adds `warning` when alerts are
  present, and `red_circle`/`yellow_circle` reflecting the worst included
  section's semaphore.

## What's deliberately out of scope here

- Sending the report (ntfy HTTP POST) — no client exists yet.
- Scheduling (cron/weekly trigger) — no scheduler exists yet.
- `readiness_events` (HRV readiness state) — not one of the four areas the
  user asked to report on; VO2max/resting HR here come from `sessions`, not
  from readiness events.
