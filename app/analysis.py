"""Server-side data analysis, ported from the phone per
docs/sync-ingestion/ANALYSIS_SPEC.md.

Every formula here is transcribed from that spec (itself a verbatim port of
the Android app's Kotlin), not invented — see the spec for the "why" behind
each threshold/window. Deviations from the spec are called out explicitly in
comments rather than silently drifting.

Structure mirrors the spec's own section numbers:
- §11 (trend aggregation + alerts) — implemented first per the spec's own
  "recommended implementation priority", since it's the actual "weekly
  analysis" deliverable; everything else is either already stored as-is or
  reference-only.
- §1 (tonnage/1RM/PR analysis) — second priority.
- §2/§3/§4/§5/§6/§7/§8/§9/§10 are either "already computed and stored"
  (nothing to port) or explicitly reference-only/out-of-scope per the spec's
  own "data availability gaps" section — not implemented here.

Everything takes a sqlite3.Connection and returns plain dataclasses/dicts —
no FastAPI dependency, so this is testable and reusable exactly like
app.parsing/app.ingestion.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# §11.1 Cardio 4-week trend card (semaphore-based)
# ---------------------------------------------------------------------------

# (source field, good direction, red predicate, green predicate)
# "good direction" / red / green mirror the spec's table exactly; None means
# "never" (a metric with no absolute red/green cutoff, i.e. always green/never red).
_CARDIO_METRICS = ("restingHr", "hrr60s", "vo2max", "rmssd", "sdnn", "drift", "poincareRatio")

_METRIC_COLUMN = {
    "restingHr": "resting_hr",
    "hrr60s": "hrr60s",
    "vo2max": "vo2max",
    "rmssd": "ecg_session_rmssd",
    "sdnn": "sdnn",
    "drift": "cardiac_drift_bpm_min",
    "poincareRatio": "poincare_ratio",
}

_GOOD_DIRECTION = {
    "restingHr": "DOWN",
    "hrr60s": "UP",
    "vo2max": "UP",
    "rmssd": "UP",
    "sdnn": "UP",
    "drift": "DOWN",
    "poincareRatio": "FLAT",
}


def _is_red(metric: str, value: float) -> bool:
    if metric == "restingHr":
        return value > 85
    if metric == "hrr60s":
        return value < 12
    if metric == "drift":
        return abs(value) > 1.0
    if metric == "poincareRatio":
        return value < 1.0 or value > 6.0
    return False  # vo2max/rmssd/sdnn: never RED per the spec's table


def _is_green(metric: str, value: float) -> bool:
    if metric == "restingHr":
        return 40 <= value <= 75
    if metric == "hrr60s":
        return value >= 20
    if metric == "drift":
        return abs(value) < 0.5
    if metric == "poincareRatio":
        return 1.5 <= value <= 4.5
    return True  # vo2max/rmssd/sdnn: always GREEN per the spec's table


def _nonzero_metric_value(metric: str, value: float) -> bool:
    """§11.1's per-metric 'source field (>0)' / '(≠0)' filters."""
    if metric == "drift":
        return value != 0
    return value > 0


@dataclass
class CardioMetricTrend:
    metric: str
    current: float
    baseline_avg: float
    delta: float
    direction: str  # UP | DOWN | FLAT
    semaphore: str  # GRAY | RED | YELLOW | GREEN
    has_enough_data: bool


@dataclass
class RhythmCounters:
    afib_episodes: int
    afib_semaphore: str
    pauses: int
    pauses_semaphore: str
    premature: int
    premature_semaphore: str
    uneven: int
    uneven_semaphore: str


@dataclass
class CardioTrendCard:
    window_end: str  # ISO date, the "today" the card was computed for
    metrics: dict[str, CardioMetricTrend]
    rhythm: RhythmCounters
    alerts: list[str]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _session_rows_in_window(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT date, resting_hr, hrr60s, vo2max, ecg_session_rmssd, sdnn,
               cardiac_drift_bpm_min, poincare_ratio, afib_suspicion_episodes,
               ecg_pause_count, ecg_pac_count, ecg_irregular_beats
        FROM sessions
        WHERE date >= ? AND date <= ? AND schema_version > 0
        ORDER BY date
        """,
        (start, end),
    ).fetchall()
    conn.row_factory = None
    return rows


def compute_cardio_trend_card(
    conn: sqlite3.Connection, *, today: str | None = None
) -> CardioTrendCard:
    """§11.1 + §11.2. `today` is an ISO date string (YYYY-MM-DD); defaults to
    the current UTC date. Window: last 28 days, split into recent (last 14)
    vs baseline (14-28 days ago)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(days=28)
    recent_start = today_date - timedelta(days=14)

    all_rows = _session_rows_in_window(conn, window_start.isoformat(), today_date.isoformat())
    recent_rows = [r for r in all_rows if r["date"] >= recent_start.isoformat()]
    baseline_rows = [r for r in all_rows if r["date"] < recent_start.isoformat()]

    has_enough_data = len(recent_rows) >= 2

    metrics: dict[str, CardioMetricTrend] = {}
    for metric in _CARDIO_METRICS:
        column = _METRIC_COLUMN[metric]
        recent_values = [
            r[column]
            for r in recent_rows
            if r[column] is not None and _nonzero_metric_value(metric, r[column])
        ]
        baseline_values = [
            r[column]
            for r in baseline_rows
            if r[column] is not None and _nonzero_metric_value(metric, r[column])
        ]

        current = _mean(recent_values)
        baseline_avg = _mean(baseline_values) if baseline_values else current
        delta = current - baseline_avg

        good_direction = _GOOD_DIRECTION[metric]

        if current == 0.0:
            direction = "FLAT"
        elif abs(delta) < abs(baseline_avg) * 0.02:
            direction = "FLAT"
        else:
            direction = "UP" if delta > 0 else "DOWN"

        if current == 0.0:
            semaphore = "GRAY"
        elif _is_red(metric, current):
            semaphore = "RED"
        elif (
            good_direction != "FLAT"
            and direction != "FLAT"
            and direction != good_direction
            and abs(delta) > abs(baseline_avg) * 0.10
        ):
            semaphore = "RED"
        elif good_direction != "FLAT" and direction != "FLAT" and direction != good_direction:
            semaphore = "YELLOW"
        elif _is_green(metric, current):
            semaphore = "GREEN"
        else:
            semaphore = "YELLOW"

        metrics[metric] = CardioMetricTrend(
            metric=metric,
            current=current,
            baseline_avg=baseline_avg,
            delta=delta,
            direction=direction,
            semaphore=semaphore,
            has_enough_data=has_enough_data,
        )

    afib = sum(r["afib_suspicion_episodes"] or 0 for r in all_rows)
    pauses = sum(r["ecg_pause_count"] or 0 for r in all_rows)
    premature = sum(r["ecg_pac_count"] or 0 for r in all_rows)
    uneven = sum(r["ecg_irregular_beats"] or 0 for r in all_rows)

    rhythm = RhythmCounters(
        afib_episodes=afib,
        afib_semaphore=_bucket(afib, green_max=0, yellow_max=2),
        pauses=pauses,
        pauses_semaphore=_bucket(pauses, green_max=0, yellow_max=2),
        premature=premature,
        premature_semaphore=_bucket(premature, green_max=20, yellow_max=60),
        uneven=uneven,
        uneven_semaphore=_bucket(uneven, green_max=30, yellow_max=100),
    )

    alerts = _self_diagnosis_alerts(
        afib_episodes=afib,
        pauses=pauses,
        recent_resting_hr=[r["resting_hr"] for r in recent_rows if r["resting_hr"]],
        baseline_resting_hr=[r["resting_hr"] for r in baseline_rows if r["resting_hr"]],
        recent_hrr=[r["hrr60s"] for r in recent_rows if r["hrr60s"]],
        total_sessions_in_window=len(all_rows),
    )

    return CardioTrendCard(
        window_end=today_date.isoformat(), metrics=metrics, rhythm=rhythm, alerts=alerts
    )


def _bucket(value: int, *, green_max: int, yellow_max: int) -> str:
    if value <= green_max:
        return "GREEN"
    if value <= yellow_max:
        return "YELLOW"
    return "RED"


# ---------------------------------------------------------------------------
# §11.2 Self-diagnosis alert strings
# ---------------------------------------------------------------------------


def _self_diagnosis_alerts(
    *,
    afib_episodes: int,
    pauses: int,
    recent_resting_hr: list[float],
    baseline_resting_hr: list[float],
    recent_hrr: list[float],
    total_sessions_in_window: int,
) -> list[str]:
    alerts: list[str] = []

    if afib_episodes > 0:
        alerts.append(
            f"{afib_episodes} AFib-suspicious episode(s) in last 4 weeks — "
            "consult a physician if recurring."
        )
    if pauses > 2:
        alerts.append(f"{pauses} pauses > 2s in last 4 weeks — uncommon, mention to doctor.")

    if len(recent_resting_hr) >= 3 and len(baseline_resting_hr) >= 3:
        d = _mean(recent_resting_hr) - _mean(baseline_resting_hr)
        if d >= 5:
            alerts.append(f"Resting HR up +{d:.0f} BPM vs baseline — possible fatigue or illness.")

    if len(recent_hrr) >= 3 and _mean(recent_hrr) < 12:
        alerts.append("HRR chronically below 12 BPM — poor recovery trend.")

    if total_sessions_in_window < 4:
        alerts.append(
            f"Only {total_sessions_in_window} session(s) in last 4 weeks — trends not yet reliable."
        )

    return alerts


# ---------------------------------------------------------------------------
# §11.3 Long-horizon weekly/monthly medians
# ---------------------------------------------------------------------------


def median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_values[mid - 1] + sorted_values[mid]) / 2
    return sorted_values[mid]


@dataclass
class WeeklyBucket:
    week_start: str  # ISO date of the Monday keying this bucket
    value: float | None
    n: int


def _monday_of(d) -> "datetime.date":
    return d - timedelta(days=d.weekday())


def _weekly_median_buckets(
    rows: list[tuple[str, float]], *, window_start, window_end
) -> list[WeeklyBucket]:
    """rows: (date_str, value) pairs already filtered per the caller's rules.
    Buckets are Monday-keyed, spanning window_start..window_end."""
    buckets: dict[str, list[float]] = {}
    cursor = _monday_of(window_start)
    while cursor <= window_end:
        buckets[cursor.isoformat()] = []
        cursor += timedelta(days=7)

    for date_str, value in rows:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        week_key = _monday_of(d).isoformat()
        if week_key in buckets:
            buckets[week_key].append(value)

    return [
        WeeklyBucket(week_start=week, value=median(values), n=len(values))
        for week, values in sorted(buckets.items())
    ]


def resting_hr_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[WeeklyBucket]:
    """8-week rolling window, median(restingHr where >0) per Monday-keyed week."""
    return _generic_weekly_trend(conn, "resting_hr", weeks=8, today=today, positive_only=True)


def hrr60s_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[WeeklyBucket]:
    """8-week rolling window, median(hrr60s where >0) per Monday-keyed week."""
    return _generic_weekly_trend(conn, "hrr60s", weeks=8, today=today, positive_only=True)


def _generic_weekly_trend(
    conn: sqlite3.Connection, column: str, *, weeks: int, today: str | None, positive_only: bool
) -> list[WeeklyBucket]:
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=weeks)

    rows = conn.execute(
        f"SELECT date, {column} FROM sessions "  # noqa: S608 — column from a fixed internal caller set
        "WHERE date >= ? AND date <= ? AND schema_version > 0 ORDER BY date",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    filtered = [(d, v) for d, v in rows if v is not None and (not positive_only or v > 0)]
    return _weekly_median_buckets(filtered, window_start=window_start, window_end=today_date)


def _weekly_sum_buckets(
    rows: list[tuple[str, float]], *, window_start, window_end
) -> list[WeeklyBucket]:
    """Same Monday-keyed bucketing as _weekly_median_buckets, but sums
    instead of taking a median — used for cumulative rhythm counters
    (afib/pauses/premature/uneven) where "how many happened this week"
    is the meaningful value, not a per-session average."""
    buckets: dict[str, list[float]] = {}
    cursor = _monday_of(window_start)
    while cursor <= window_end:
        buckets[cursor.isoformat()] = []
        cursor += timedelta(days=7)

    for date_str, value in rows:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        week_key = _monday_of(d).isoformat()
        if week_key in buckets:
            buckets[week_key].append(value)

    return [
        WeeklyBucket(week_start=week, value=sum(values) if values else 0.0, n=len(values))
        for week, values in sorted(buckets.items())
    ]


# All 7 cardio metrics (§11.1) plus the 4 rhythm counters, as weekly time
# series rather than a single recent-vs-baseline aggregate — same 12-week
# window ANALYSIS_SPEC.md §1.6 already uses for session tonnage/e1RM trend
# charts, so every chart on the dashboard spans a consistent period.
_CARDIO_SERIES_WEEKS = 12

# (key, column, aggregation, positive_only) — aggregation "median" mirrors
# the trend-card's per-metric semantics (average condition over the week);
# "sum" mirrors the trend-card's rhythm counters (cumulative incidents).
_CARDIO_SERIES_SPECS = (
    ("restingHr", "resting_hr", "median", True),
    ("hrr60s", "hrr60s", "median", True),
    ("vo2max", "vo2max", "median", True),
    ("rmssd", "ecg_session_rmssd", "median", True),
    ("sdnn", "sdnn", "median", True),
    ("pnn50", "pnn50", "median", True),
    ("drift", "cardiac_drift_bpm_min", "median", False),  # can be negative
    ("poincareRatio", "poincare_ratio", "median", True),
    ("afibEpisodes", "afib_suspicion_episodes", "sum", False),
    ("pauses", "ecg_pause_count", "sum", False),
    ("premature", "ecg_pac_count", "sum", False),
    ("uneven", "ecg_irregular_beats", "sum", False),
)


def all_cardio_metrics_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[WeeklyBucket]]:
    """Weekly time series for every §11.1 metric and rhythm counter, so each
    can be charted rather than shown only as a single 4-week aggregate."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_CARDIO_SERIES_WEEKS)

    result: dict[str, list[WeeklyBucket]] = {}
    for key, column, aggregation, positive_only in _CARDIO_SERIES_SPECS:
        rows = conn.execute(
            f"SELECT date, {column} FROM sessions "  # noqa: S608 — column from the fixed spec table above
            "WHERE date >= ? AND date <= ? AND schema_version > 0 ORDER BY date",
            (window_start.isoformat(), today_date.isoformat()),
        ).fetchall()
        filtered = [(d, v) for d, v in rows if v is not None and (not positive_only or v > 0)]
        bucket_fn = _weekly_sum_buckets if aggregation == "sum" else _weekly_median_buckets
        result[key] = bucket_fn(filtered, window_start=window_start, window_end=today_date)

    return result


@dataclass
class LatestReadiness:
    measured_at: str
    readiness: str
    ln_rmssd: float | None
    resting_hr: int | None
    vo2max: float | None
    recommendation: str


def latest_readiness(conn: sqlite3.Connection) -> LatestReadiness | None:
    """Most recent daily readiness measurement (a separate phone-side flow
    from gym sessions — see docs/sync-ingestion/READINESS_SPEC.md), for a
    single 'how am I doing today' card rather than a trend."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT measured_at, readiness, ln_rmssd, resting_hr, vo2max, recommendation
           FROM readiness_events ORDER BY measured_at DESC LIMIT 1"""
    ).fetchone()
    conn.row_factory = None
    if row is None:
        return None
    return LatestReadiness(
        measured_at=row["measured_at"],
        readiness=row["readiness"],
        ln_rmssd=row["ln_rmssd"],
        resting_hr=row["resting_hr"],
        vo2max=row["vo2max"],
        recommendation=row["recommendation"],
    )


@dataclass
class ReadinessPoint:
    date: str  # calendar date (measured_at truncated to YYYY-MM-DD)
    readiness: str | None  # DELOAD_RECOMMENDED|LIGHT_DAY|NORMAL|GOOD|PEAK|NO_BASELINE


def readiness_daily_trend(
    conn: sqlite3.Connection, *, days: int = 30, today: str | None = None
) -> list[ReadinessPoint]:
    """Daily readiness state from readiness_events over the trailing `days`
    days. Plots the phone's own classification (readiness_events.readiness)
    as-is — the phone computes this from a rolling baseline it doesn't send
    us the history for, so the server has no way to recompute it; showing
    the raw ln(RMSSD) number or trying to re-derive a z-score from
    server-side history alone was tried and was wrong (too little history
    synced yet to match the phone's baseline, and it hid data that does
    exist). This is a category, not a number: DELOAD_RECOMMENDED < LIGHT_DAY
    < NORMAL < GOOD < PEAK — the dashboard plots it on an ordinal axis, not
    a continuous one."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(days=days)

    rows = conn.execute(
        """SELECT measured_at, readiness FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
           ORDER BY measured_at""",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    return [
        ReadinessPoint(date=measured_at[:10], readiness=readiness)
        for measured_at, readiness in rows
    ]


def vo2max_4week_trend(conn: sqlite3.Connection, *, today: str | None = None) -> list[WeeklyBucket]:
    """§11.3: 4-week buckets (not weekly) over a 6-month fetch window, bucket
    boundary = window_start + floor(weeks_since_start / 4) * 4 weeks,
    value = median(vo2max where >0)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(days=182)  # ~6 months

    rows = conn.execute(
        "SELECT date, vo2max FROM sessions "
        "WHERE date >= ? AND date <= ? AND schema_version > 0 ORDER BY date",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    buckets: dict[str, list[float]] = {}
    cursor = window_start
    while cursor <= today_date:
        buckets[cursor.isoformat()] = []
        cursor += timedelta(weeks=4)

    bucket_starts = sorted(datetime.strptime(k, "%Y-%m-%d").date() for k in buckets)
    for date_str, vo2max in rows:
        if vo2max is None or vo2max <= 0:
            continue
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        weeks_since_start = (d - window_start).days // 7
        bucket_index = weeks_since_start // 4
        bucket_start = window_start + timedelta(weeks=bucket_index * 4)
        key = bucket_start.isoformat()
        if key in buckets:
            buckets[key].append(vo2max)
        else:
            # Falls exactly on/after the last computed boundary — bucket it there.
            nearest = max((b for b in bucket_starts if b <= d), default=bucket_starts[0])
            buckets[nearest.isoformat()].append(vo2max)

    return [
        WeeklyBucket(week_start=week, value=median(values), n=len(values))
        for week, values in sorted(buckets.items())
    ]


def scale_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[WeeklyBucket]]:
    """§11.3: 8-week rolling window, weekly (Monday-keyed) medians for
    weightKg, bmi (>0), bodyFatPercent (>0), leanMassPercent (only weeks with
    a real BIA reading, i.e. bodyFatPercent > 0)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=8)

    rows = conn.execute(
        "SELECT date, weight_kg, bmi, body_fat_percent, lean_mass_percent FROM scale_weighins "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    weight = [(d, w) for d, w, *_ in rows if w is not None]
    bmi = [(d, b) for d, _, b, _, _ in rows if b is not None and b > 0]
    body_fat = [(d, bf) for d, _, _, bf, _ in rows if bf is not None and bf > 0]
    lean_mass = [
        (d, lm) for d, _, _, bf, lm in rows if bf is not None and bf > 0 and lm is not None
    ]

    return {
        "weightKg": _weekly_median_buckets(
            weight, window_start=window_start, window_end=today_date
        ),
        "bmi": _weekly_median_buckets(bmi, window_start=window_start, window_end=today_date),
        "bodyFatPercent": _weekly_median_buckets(
            body_fat, window_start=window_start, window_end=today_date
        ),
        "leanMassPercent": _weekly_median_buckets(
            lean_mass, window_start=window_start, window_end=today_date
        ),
    }


# ---------------------------------------------------------------------------
# §11.4 Average weekly delta
# ---------------------------------------------------------------------------


def average_weekly_delta(series: list[float]) -> float | None:
    """Only the endpoints matter (algebraically equivalent to averaging
    consecutive deltas) — null if fewer than 2 points."""
    if len(series) < 2:
        return None
    return (series[-1] - series[0]) / (len(series) - 1)


# ---------------------------------------------------------------------------
# §1.4 Estimated 1RM — Epley formula
# ---------------------------------------------------------------------------


def estimate_1rm(weight: float, reps: int) -> float:
    return weight * (1 + reps / 30.0)


@dataclass
class SetLike:
    reps: int
    weight: float
    # See app.parsing's isBodyweight note: distinguishes a completed
    # bodyweight set (weight=0 by nature) from an untouched/empty one, which
    # otherwise look identical. Only consumed by _is_untouched_or_empty below
    # — weight-based PR/1RM calculations correctly stay 0/None for bodyweight
    # sets regardless of this flag, since there's no external load to PR on.
    is_bodyweight: bool = False


def best_estimated_1rm(sets: list[SetLike]) -> float | None:
    qualifying = [estimate_1rm(s.weight, s.reps) for s in sets if s.reps > 0 and s.weight > 0]
    return max(qualifying) if qualifying else None


# ---------------------------------------------------------------------------
# §1.5 PR detection — two distinct definitions, kept separate per the spec
# ---------------------------------------------------------------------------


@dataclass
class ExerciseSummary:
    exercise_id: str
    exercise_name: str | None
    set_count: int


def list_exercises(conn: sqlite3.Connection) -> list[ExerciseSummary]:
    """Distinct exercises with at least one synced set, most-logged first.
    Not part of ANALYSIS_SPEC.md — added for the GUI's exercise picker, so a
    user can find an exercise_id without knowing it in advance."""
    rows = conn.execute(
        """
        SELECT exercise_id, MAX(exercise_name), COUNT(*) as c
        FROM exercise_sets
        GROUP BY exercise_id
        ORDER BY c DESC
        """
    ).fetchall()
    return [ExerciseSummary(exercise_id=r[0], exercise_name=r[1], set_count=r[2]) for r in rows]


@dataclass
class TonnagePR:
    exercise_id: str
    reps: int
    weight: float


def all_time_tonnage_pr(conn: sqlite3.Connection, exercise_id: str) -> TonnagePR | None:
    """All-time best single set (reps*weight), across every FORZA set ever
    recorded for this exercise, excluding excludeFromTonnage sets."""
    row = conn.execute(
        """
        SELECT reps, weight FROM exercise_sets
        WHERE exercise_id = ? AND exclude_from_tonnage = 0 AND reps > 0 AND weight > 0
        ORDER BY (reps * weight) DESC
        LIMIT 1
        """,
        (exercise_id,),
    ).fetchone()
    if row is None:
        return None
    return TonnagePR(exercise_id=exercise_id, reps=row[0], weight=row[1])


@dataclass
class RecentHeaviestPR:
    exercise_id: str
    reps: int
    weight: float
    session_id: str
    session_date: str


def recent_heaviest_set_pr(
    conn: sqlite3.Connection,
    exercise_id: str,
    *,
    is_daily: bool,
    is_warmup: bool,
    limit_sessions: int = 30,
) -> RecentHeaviestPR | None:
    """Last 30 sessions containing this exercise, matched like-for-like on
    is_daily and (exclude_from_tonnage and not is_daily) == is_warmup.
    Best set = max(weight, reps) lexicographically (weight first)."""
    rows = conn.execute(
        """
        SELECT es.reps, es.weight, es.session_id, s.date
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE es.exercise_id = ?
          AND es.is_daily = ?
          AND (es.exclude_from_tonnage = 1 AND es.is_daily = 0) = ?
          AND es.weight > 0
        ORDER BY s.date DESC
        LIMIT ?
        """,
        (
            exercise_id,
            int(is_daily),
            int(is_warmup),
            limit_sessions * 20,
        ),  # generous cap on rows scanned
    ).fetchall()
    if not rows:
        return None

    # Restrict to the most recent `limit_sessions` distinct sessions (rows
    # above may include multiple sets per session).
    seen_sessions: list[str] = []
    for _reps, _weight, session_id, _date in rows:
        if session_id not in seen_sessions:
            seen_sessions.append(session_id)
        if len(seen_sessions) > limit_sessions:
            break
    allowed_sessions = set(seen_sessions[:limit_sessions])

    candidates = [r for r in rows if r[2] in allowed_sessions]
    best = max(candidates, key=lambda r: (r[1], r[0]))  # (weight, reps) lexicographic
    return RecentHeaviestPR(
        exercise_id=exercise_id,
        reps=best[0],
        weight=best[1],
        session_id=best[2],
        session_date=best[3],
    )


@dataclass
class ExerciseSessionPoint:
    session_date: str
    session_id: str
    best_set_weight: float  # heaviest working set that session, 0 if none
    best_e1rm: float | None  # None if no qualifying set (see best_estimated_1rm)


def exercise_history(
    conn: sqlite3.Connection, exercise_id: str, *, weeks: int = 12, today: str | None = None
) -> list[ExerciseSessionPoint]:
    """§1.6-style trend series for a single exercise: one point per session
    that included it, chronological, over a rolling window (12 weeks by
    default, matching ANALYSIS_SPEC.md §1.6's session tonnage/e1RM trend
    window). Not itself in the spec — this is the per-exercise chart the
    dashboard needs, built from the same best_estimated_1rm/set data the
    spec's PR functions already use."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=weeks)

    rows = conn.execute(
        """
        SELECT s.date, s.id, es.reps, es.weight
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE es.exercise_id = ? AND es.exclude_from_tonnage = 0
          AND s.date >= ? AND s.date <= ?
        ORDER BY s.date
        """,
        (exercise_id, window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    by_session: dict[tuple[str, str], list[SetLike]] = {}
    for date_str, session_id, reps, weight in rows:
        by_session.setdefault((date_str, session_id), []).append(SetLike(reps=reps, weight=weight))

    points = [
        ExerciseSessionPoint(
            session_date=date_str,
            session_id=session_id,
            best_set_weight=max((s.weight for s in sets if s.weight > 0), default=0.0),
            best_e1rm=best_estimated_1rm(sets),
        )
        for (date_str, session_id), sets in sorted(by_session.items())
    ]
    return points


# ---------------------------------------------------------------------------
# §1.2 Common-tonnage comparison between two sessions
# ---------------------------------------------------------------------------


@dataclass
class ExerciseOccurrence:
    exercise_id: str
    exclude_from_tonnage: bool
    sets: list[SetLike]


def _is_untouched_or_empty(exercise: ExerciseOccurrence) -> bool:
    """Approximation of the phone's `completed=False and sets=[]` /
    `completed with nothing touched` predicates: the raw-synced schema
    doesn't carry a `completed` flag (see docs/sync-ingestion/SPEC.md — it
    was never part of the wire format), so this treats "no sets with
    reps>0 and weight>0" as the observable proxy for both cases. Weaker
    than the phone's true flag, but the closest available signal from what's
    actually synced today.

    A set with `is_bodyweight=True` counts as touched even at weight=0 —
    once the phone sends that field (see app.parsing's isBodyweight note),
    this stops misclassifying completed bodyweight work as untouched."""
    return not any(s.reps > 0 and (s.weight > 0 or s.is_bodyweight) for s in exercise.sets)


def common_tonnage(
    exercises1: list[ExerciseOccurrence], exercises2: list[ExerciseOccurrence]
) -> tuple[float, float]:
    def eligible(exercises: list[ExerciseOccurrence]) -> set[str]:
        return {
            e.exercise_id
            for e in exercises
            if not e.exclude_from_tonnage and not _is_untouched_or_empty(e)
        }

    common_ids = eligible(exercises1) & eligible(exercises2)
    if not common_ids:
        total1 = sum(
            s.reps * s.weight for e in exercises1 for s in e.sets if not e.exclude_from_tonnage
        )
        total2 = sum(
            s.reps * s.weight for e in exercises2 for s in e.sets if not e.exclude_from_tonnage
        )
        return (total1, total2)

    def tonnage_for(exercises: list[ExerciseOccurrence]) -> float:
        return sum(
            s.reps * s.weight
            for e in exercises
            if e.exercise_id in common_ids and not e.exclude_from_tonnage
            for s in e.sets
        )

    return (tonnage_for(exercises1), tonnage_for(exercises2))


def tonnage_change_percent(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current - previous) / previous * 100.0


# ---------------------------------------------------------------------------
# §1.3 Per-exercise change % within a session vs the prior one
# ---------------------------------------------------------------------------


@dataclass
class ExerciseChange:
    exercise_id: str
    prev_tonnage: float
    curr_tonnage: float
    change_percent: float | None
    is_first_time: bool


def per_exercise_change(
    exercise_id: str, prev_sets: list[SetLike], curr_sets: list[SetLike]
) -> ExerciseChange:
    prev_tonnage = sum(s.reps * s.weight for s in prev_sets)
    curr_tonnage = sum(s.reps * s.weight for s in curr_sets)
    change_percent = (
        (curr_tonnage - prev_tonnage) / prev_tonnage * 100 if prev_tonnage > 0 else None
    )
    is_first_time = prev_tonnage == 0 and curr_tonnage > 0
    return ExerciseChange(
        exercise_id=exercise_id,
        prev_tonnage=prev_tonnage,
        curr_tonnage=curr_tonnage,
        change_percent=change_percent,
        is_first_time=is_first_time,
    )
