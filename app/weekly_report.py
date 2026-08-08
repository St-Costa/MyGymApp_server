"""Weekly health/training report — content generation only.

Builds a deterministic (no LLM), ntfy-shaped summary of the most recently
completed Monday-Sunday week: what went well, what didn't, and what's just
"as expected" and therefore not worth a line. See docs/weekly-report/README.md
for the inclusion/silence rule and full section-by-section behaviour.

This module only produces text/priority/tags — no ntfy HTTP client, no
scheduler, no new config. Those are a future module. Like app.analysis, this
takes a sqlite3.Connection and returns plain dataclasses, no FastAPI
dependency, so it's testable and reusable exactly the same way.

Reuses app.analysis wherever the numbers already exist there (cardio trend
card + alerts, ACWR/RPE-ACWR semaphores, scale weekly buckets, PR lookups) —
only what's genuinely new lives here: the exact-week queries app.analysis
doesn't do at that granularity, new-PR-this-week detection, and the
inclusion/silence + rendering logic.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import timedelta

from app.analysis import (
    AcwrTrend,
    CardioTrendCard,
    acwr_daily_trend,
    all_time_tonnage_pr,
    compute_acwr_trend,
    compute_cardio_trend_card,
    compute_rpe_acwr_trend,
    list_exercises,
    median,
    rpe_acwr_daily_trend,
    scale_weekly_trend,
)

_SEMAPHORE_EMOJI = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "GRAY": "⚪"}
_DIRECTION_ARROW = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}

# Cardio metrics carry a human label + unit for section text; VO2max is
# reported separately (as an exact number, not via this synthetic section),
# so it's excluded here even though compute_cardio_trend_card computes it too.
_CARDIO_METRIC_LABELS = {
    "restingHr": "FC a riposo",
    "hrr60s": "Recupero cardiaco (HRR60s)",
    "rmssd": "RMSSD",
    "sdnn": "SDNN",
    "drift": "Deriva cardiaca",
    "poincareRatio": "Poincaré ratio",
}


@dataclass
class WeeklyReportSection:
    """One block of the report. `include=False` means "as expected, silenced
    on purpose" — see the module docstring's inclusion rule, not an error or
    missing data (missing data still gets include=True with an explicit
    "no data" headline, per the raw-first philosophy: silence must never be
    ambiguous with a bug)."""

    key: str  # "weight" | "vo2max" | "training_load" | "cardio_volume"
    include: bool
    headline: str | None
    semaphore: str | None = None  # GRAY|RED|YELLOW|GREEN, only where applicable


@dataclass
class WeeklyReport:
    week_start: str  # ISO date, the Monday
    week_end: str  # ISO date, the Sunday
    title: str
    body: str
    priority: int  # 1-5, ntfy convention: higher when RED/alerts are present
    tags: list[str]  # ntfy emoji-shortcode tags
    sections: list[WeeklyReportSection]
    alerts: list[str]  # verbatim from compute_cardio_trend_card's self-diagnosis


def _last_completed_week(today: date_cls) -> tuple[date_cls, date_cls]:
    """The most recently finished Monday-Sunday week as of `today` — if the
    job runs any day this week, it reports the week before, never a partial
    week still in progress."""
    this_monday = today - timedelta(days=today.weekday())
    week_end = this_monday - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    return week_start, week_end


def _session_stats_for_week(
    conn: sqlite3.Connection, week_start: date_cls, week_end: date_cls
) -> tuple[int, float]:
    """Count and total tonnage of parsed sessions strictly within the
    reported week — no existing app.analysis function does exact-week
    granularity (its weekly buckets are Monday-keyed but span 8-12 week
    rolling windows, not a single isolated week)."""
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(total_tonnage), 0)
        FROM sessions
        WHERE date >= ? AND date <= ? AND schema_version > 0
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchone()
    return row[0], row[1]


def _vo2max_for_week(
    conn: sqlite3.Connection, week_start: date_cls, week_end: date_cls
) -> float | None:
    """Median vo2max from sessions strictly within the reported week — the
    "exact number for THIS week" the user asked for, distinct from
    vo2max_4week_trend's coarser 4-week buckets."""
    rows = conn.execute(
        """
        SELECT vo2max FROM sessions
        WHERE date >= ? AND date <= ? AND schema_version > 0
          AND vo2max IS NOT NULL AND vo2max > 0
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    return median([r[0] for r in rows])


def _new_prs_this_week(
    conn: sqlite3.Connection, week_start: date_cls, week_end: date_cls
) -> list[str]:
    """Exercises where this week's best single-set tonnage (reps*weight)
    equals the all-time PR — i.e. a PR was set (not just approached) this
    week. Equality, not >=, matches all_time_tonnage_pr's own
    "ORDER BY reps*weight DESC LIMIT 1": the all-time PR *is* this week's
    best set exactly when they're the same row (or an exact tie)."""
    rows = conn.execute(
        """
        SELECT es.exercise_id, MAX(es.reps * es.weight)
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE s.date >= ? AND s.date <= ? AND s.schema_version > 0
          AND es.exclude_from_tonnage = 0 AND es.reps > 0 AND es.weight > 0
        GROUP BY es.exercise_id
        """,
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()

    names = {e.exercise_id: e.exercise_name for e in list_exercises(conn)}
    new_prs = []
    for exercise_id, week_best in rows:
        pr = all_time_tonnage_pr(conn, exercise_id)
        # Both sides come from the same reps*weight arithmetic over the same
        # underlying rows (the all-time PR row is necessarily one of this
        # week's rows when it was set this week), so exact float equality is
        # safe here — no independent computation on either side to drift.
        if pr is not None and week_best == pr.reps * pr.weight:
            label = names.get(exercise_id) or exercise_id
            new_prs.append(f"Nuovo PR: {label} ({pr.weight:.1f}kg x {pr.reps})")
    return new_prs


def _weight_section(
    conn: sqlite3.Connection, week_start: date_cls, week_end: date_cls
) -> WeeklyReportSection:
    """Peso/BMI/BF%/lean-mass: always included verbatim when present — this
    is one of the two "always report exact numbers" areas per the user's
    explicit priority, never silenced even when nothing changed."""
    # scale_weekly_trend's buckets are Monday-keyed over an 8-week window
    # ending at `today` — passing week_end (the reported week's Sunday) as
    # `today` guarantees the window includes every day of the reported week,
    # and the reported week's own bucket is keyed by its Monday.
    buckets = scale_weekly_trend(conn, today=week_end.isoformat())
    week_key = week_start.isoformat()

    def _value(metric: str) -> float | None:
        bucket = next((b for b in buckets[metric] if b.week_start == week_key), None)
        return bucket.value if bucket else None

    weight = _value("weightKg")
    if weight is None:
        return WeeklyReportSection(
            key="weight", include=True, headline="Peso: nessuna pesata questa settimana"
        )

    bmi = _value("bmi")
    body_fat = _value("bodyFatPercent")
    lean_mass = _value("leanMassPercent")

    parts = [f"{weight:.1f} kg"]
    details = []
    if bmi is not None:
        details.append(f"BMI {bmi:.1f}")
    if body_fat is not None:
        details.append(f"BF {body_fat:.1f}%")
    if lean_mass is not None:
        details.append(f"massa magra {lean_mass:.1f}%")
    if details:
        parts.append(f"({', '.join(details)})")

    return WeeklyReportSection(key="weight", include=True, headline=f"Peso: {' '.join(parts)}")


def _vo2max_section(
    conn: sqlite3.Connection, week_start: date_cls, week_end: date_cls
) -> WeeklyReportSection:
    """VO2max: the other "always report exact numbers" area — same rule as
    weight, never silenced."""
    vo2max = _vo2max_for_week(conn, week_start, week_end)
    if vo2max is None:
        return WeeklyReportSection(
            key="vo2max", include=True, headline="VO2max: nessun dato questa settimana"
        )
    return WeeklyReportSection(
        key="vo2max", include=True, headline=f"VO2max: {vo2max:.1f} ml/kg/min"
    )


def _acwr_direction(conn: sqlite3.Connection, week_end: date_cls, daily_trend_fn) -> str:
    """ACWR/RPE-ACWR week-over-week direction. AcwrTrend has no native
    `direction` field (unlike CardioMetricTrend) — derive it from two
    consecutive Monday-keyed points of the *_daily_trend series, which is
    cheaper and more consistent than calling compute_*_acwr_trend twice with
    different `today` values."""
    points = daily_trend_fn(conn, today=week_end.isoformat())
    this_monday = (week_end - timedelta(days=6)).isoformat()
    by_date = {p.date: p for p in points}
    current = by_date.get(this_monday)
    previous_monday = (week_end - timedelta(days=13)).isoformat()
    previous = by_date.get(previous_monday)

    if current is None or current.acwr is None or previous is None or previous.acwr is None:
        return "FLAT"
    delta = current.acwr - previous.acwr
    if abs(delta) < 0.02:  # mirrors compute_cardio_trend_card's noise-floor convention
        return "FLAT"
    return "UP" if delta > 0 else "DOWN"


def _training_load_section(conn: sqlite3.Connection, week_end: date_cls) -> WeeklyReportSection:
    """ACWR tonnage + RPE-ACWR: synthetic status only (semaphore + arrow),
    never the raw ratio — per the user's explicit low-priority-for-numbers
    request on this area. Silenced when both are GREEN and stable."""
    tonnage_trend: AcwrTrend = compute_acwr_trend(conn, today=week_end.isoformat())
    rpe_trend: AcwrTrend = compute_rpe_acwr_trend(conn, today=week_end.isoformat())
    tonnage_direction = _acwr_direction(conn, week_end, acwr_daily_trend)
    rpe_direction = _acwr_direction(conn, week_end, rpe_acwr_daily_trend)

    notable = (
        tonnage_trend.semaphore != "GREEN"
        or tonnage_direction != "FLAT"
        or rpe_trend.semaphore != "GREEN"
        or rpe_direction != "FLAT"
    )
    # Report whichever of the two states is "worse" as the section's own
    # semaphore for priority/tag purposes — GRAY < GREEN < YELLOW < RED.
    rank = {"GRAY": 0, "GREEN": 1, "YELLOW": 2, "RED": 3}
    worst_semaphore = max((tonnage_trend.semaphore, rpe_trend.semaphore), key=lambda s: rank[s])

    if not notable:
        return WeeklyReportSection(
            key="training_load", include=False, headline=None, semaphore=worst_semaphore
        )

    lines = [
        f"Carico allenante (tonnage): {_SEMAPHORE_EMOJI[tonnage_trend.semaphore]} "
        f"{_DIRECTION_ARROW[tonnage_direction]}",
        f"Carico allenante (RPE): {_SEMAPHORE_EMOJI[rpe_trend.semaphore]} "
        f"{_DIRECTION_ARROW[rpe_direction]}",
    ]
    return WeeklyReportSection(
        key="training_load",
        include=True,
        headline="\n".join(lines),
        semaphore=worst_semaphore,
    )


def _cardio_volume_section(
    conn: sqlite3.Connection,
    week_start: date_cls,
    week_end: date_cls,
    card: CardioTrendCard,
) -> WeeklyReportSection:
    """Cardio/HRV synthetic status (only non-GREEN/non-FLAT metrics get a
    line) plus session count/tonnage (always shown, even at zero — an
    empty week is itself a signal, not noise to hide) and new PRs."""
    n_sessions, total_tonnage = _session_stats_for_week(conn, week_start, week_end)
    new_prs = _new_prs_this_week(conn, week_start, week_end)

    # GRAY here means "this metric has no data at all this window" (e.g. no
    # ECG ever recorded), not "something's wrong" — surfacing it as a status
    # line would be noise, not signal, and would keep the section permanently
    # "notable" for a metric the user simply never populates. Only YELLOW/RED
    # (a real, evaluable status) or a direction change count as notable.
    notable_metrics = [
        m
        for name, m in card.metrics.items()
        if name in _CARDIO_METRIC_LABELS
        and m.semaphore != "GRAY"
        and (m.semaphore != "GREEN" or m.direction != "FLAT")
    ]

    lines = []
    if n_sessions == 0:
        lines.append("Nessuna sessione questa settimana")
    else:
        lines.append(f"{n_sessions} sessioni, {total_tonnage:.0f} kg totali")

    for m in notable_metrics:
        label = _CARDIO_METRIC_LABELS[m.metric]
        lines.append(f"{label}: {_SEMAPHORE_EMOJI[m.semaphore]} {_DIRECTION_ARROW[m.direction]}")

    lines.extend(new_prs)

    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    worst_semaphore = (
        max((m.semaphore for m in notable_metrics), key=lambda s: rank[s])
        if notable_metrics
        else "GREEN"
    )

    return WeeklyReportSection(
        key="cardio_volume", include=True, headline="\n".join(lines), semaphore=worst_semaphore
    )


def _compute_priority(sections: list[WeeklyReportSection], alerts: list[str]) -> int:
    """ntfy priority convention: 1 (min) .. 5 (max/urgent). Alerts (medical
    self-diagnosis strings) always push to the top; otherwise driven by the
    worst semaphore across sections that were actually included."""
    if alerts:
        return 5
    rank = {"GRAY": 0, "GREEN": 1, "YELLOW": 2, "RED": 3}
    included_semaphores = [s.semaphore for s in sections if s.include and s.semaphore]
    if not included_semaphores:
        return 3  # default/normal
    worst = max(included_semaphores, key=lambda s: rank[s])
    return {"GRAY": 3, "GREEN": 3, "YELLOW": 4, "RED": 5}[worst]


def _compute_tags(sections: list[WeeklyReportSection], alerts: list[str]) -> list[str]:
    tags = ["bar_chart"]
    if alerts:
        tags.append("warning")
    if any(s.include and s.semaphore == "RED" for s in sections):
        tags.append("red_circle")
    elif any(s.include and s.semaphore == "YELLOW" for s in sections):
        tags.append("yellow_circle")
    return tags


def build_weekly_report(
    conn: sqlite3.Connection, *, week_end: date_cls | None = None
) -> WeeklyReport:
    """Builds the full weekly report. `week_end` is the Sunday of the
    reported week; defaults to the most recently completed week as of
    today. Always pass it explicitly in tests to avoid depending on the
    wall clock."""
    if week_end is None:
        week_start, week_end = _last_completed_week(date_cls.today())
    else:
        week_start = week_end - timedelta(days=6)

    card = compute_cardio_trend_card(conn, today=week_end.isoformat())

    weight_section = _weight_section(conn, week_start, week_end)
    vo2max_section = _vo2max_section(conn, week_start, week_end)
    training_load_section = _training_load_section(conn, week_end)
    cardio_volume_section = _cardio_volume_section(conn, week_start, week_end, card)

    sections = [weight_section, vo2max_section, training_load_section, cardio_volume_section]

    # cardio_volume_section is always include=True (it always reports session
    # count), so "everything quiet" is judged by its semaphore alongside
    # training_load's include flag — the section itself can't go silent
    # (session count/PRs must always be visible), only its "notable metrics"
    # sub-lines can be absent, which shows up as its semaphore being GREEN.
    all_quiet = not training_load_section.include and cardio_volume_section.semaphore == "GREEN"

    body_lines = [weight_section.headline, vo2max_section.headline]
    if training_load_section.include:
        body_lines.append(training_load_section.headline)
    body_lines.append(cardio_volume_section.headline)
    if all_quiet:
        body_lines.append("Tutto in norma questa settimana 👍")
    body_lines.extend(card.alerts)

    return WeeklyReport(
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        title=f"Report settimanale {week_start.isoformat()} → {week_end.isoformat()}",
        body="\n".join(line for line in body_lines if line),
        priority=_compute_priority(sections, card.alerts),
        tags=_compute_tags(sections, card.alerts),
        sections=sections,
        alerts=card.alerts,
    )
