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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.ecg_analysis import estimated_max_hr


def round_floats(value, ndigits: int = 2):
    """Recursively round every float in a JSON-shaped structure (dict / list /
    scalar) to `ndigits` decimals. The functions in this module compute
    ratios, rolling means and semaphore deltas that come out with long
    floating-point tails carrying no real precision — 2dp is all any
    consumer (the /v1/analysis/* JSON export, the server-rendered dashboard)
    needs. Applied once at each output boundary (app.routes, app.gui) rather
    than sprinkled through every formula here. bool passes straight through
    (it is an int subclass, not a float)."""
    if isinstance(value, float):
        return round(value, ndigits)
    if isinstance(value, dict):
        return {k: round_floats(v, ndigits) for k, v in value.items()}
    if isinstance(value, list):
        return [round_floats(v, ndigits) for v in value]
    return value


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


# ecg_analysis's tier-1 advanced metrics (docs/sync-ingestion/
# ECG_ADVANCED_ANALYSIS.md), as weekly time series — same shape/window as
# _CARDIO_SERIES_SPECS above, but sourced from ecg_analysis (joined against
# sessions for its date) rather than sessions' own columns. Deliberately
# only the interpretable physiological metrics, not every column that
# table has: kurtosis_sqi/b_sqi are signal-quality diagnostics, not
# fitness trends, and csi/cvi are redundant with hrv_lf_hf (same
# sympathovagal-balance signal) — see ECG_ADVANCED_ANALYSIS.md.
_ECG_ADVANCED_SERIES_WEEKS = _CARDIO_SERIES_WEEKS

_ECG_ADVANCED_SERIES_SPECS = (
    ("dfaAlpha1", "dfa_alpha1"),
    ("hrvLfHf", "hrv_lf_hf"),
    ("sampleEntropy", "sample_entropy"),
    ("hrr1min", "hrr_1min"),
    ("hrr2min", "hrr_2min"),
)


def ecg_advanced_metrics_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[WeeklyBucket]]:
    """Weekly median time series for each tier-1 advanced ECG metric.
    INNER JOIN against sessions (for its date) naturally excludes ECG
    recordings with no matching session — e.g. the phone's debug-send
    button uses synthetic `debug-*` ids (see ECG_SPEC.md point 5), which
    are test uploads, not real workouts, and shouldn't appear in a fitness
    trend. Every field here is nullable at the row level (see
    EcgAnalysisResult's "no result" convention) — the median naturally
    ignores weeks where a given metric couldn't be computed, same as the
    cardio metrics' own positive_only/None filtering above."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_ECG_ADVANCED_SERIES_WEEKS)

    result: dict[str, list[WeeklyBucket]] = {}
    for key, column in _ECG_ADVANCED_SERIES_SPECS:
        rows = conn.execute(
            f"""SELECT sessions.date, ecg_analysis.{column}
                FROM ecg_analysis
                JOIN sessions ON sessions.id = ecg_analysis.session_id
                WHERE sessions.date >= ? AND sessions.date <= ?
                ORDER BY sessions.date""",  # noqa: S608 — column from the fixed spec table above
            (window_start.isoformat(), today_date.isoformat()),
        ).fetchall()
        filtered = [(d, v) for d, v in rows if v is not None]
        result[key] = _weekly_median_buckets(
            filtered, window_start=window_start, window_end=today_date
        )

    return result


# HR-zone minutes (app.ecg_analysis.compute_hr_zone_minutes) — summed per
# week, not medianed, same "how much happened this week" reasoning as the
# rhythm counters' _weekly_sum_buckets: total minutes in a zone across all
# of a week's sessions is the meaningful value for a stacked-bar chart, not
# a per-session average. Own (shorter) window than the other ECG advanced
# series — 6 stacked segments per bar reads as clutter past ~8 bars, per
# user decision, unlike a single-line chart where 12 weeks reads fine.
_HR_ZONE_SERIES_WEEKS = 8

_HR_ZONE_COLUMNS = (
    ("zone1", "hr_zone1_min"),
    ("zone2", "hr_zone2_min"),
    ("zone3", "hr_zone3_min"),
    ("zone4", "hr_zone4_min"),
    ("zone5", "hr_zone5_min"),
    ("belowZone1", "hr_below_zone1_min"),
)


def hr_zones_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[WeeklyBucket]]:
    """Weekly summed minutes-in-zone, one series per zone, over the trailing
    _HR_ZONE_SERIES_WEEKS weeks — same INNER JOIN/debug-id-exclusion
    reasoning as ecg_advanced_metrics_weekly_trend above. Meant to be
    charted together as a stacked bar (one bar per week, split into its 6
    zone segments), not as 6 independent line charts."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_HR_ZONE_SERIES_WEEKS)

    result: dict[str, list[WeeklyBucket]] = {}
    for key, column in _HR_ZONE_COLUMNS:
        rows = conn.execute(
            f"""SELECT sessions.date, ecg_analysis.{column}
                FROM ecg_analysis
                JOIN sessions ON sessions.id = ecg_analysis.session_id
                WHERE sessions.date >= ? AND sessions.date <= ?
                ORDER BY sessions.date""",  # noqa: S608 — column from the fixed spec table above
            (window_start.isoformat(), today_date.isoformat()),
        ).fetchall()
        filtered = [(d, v) for d, v in rows if v is not None]
        result[key] = _weekly_sum_buckets(
            filtered, window_start=window_start, window_end=today_date
        )

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


def resting_hr_7day_average(conn: sqlite3.Connection, *, as_of_date: str) -> float | None:
    """7-day trailing average of morning resting HR (readiness_events.
    resting_hr), ending on `as_of_date` inclusive — feeds app.ecg_analysis's
    %HRR (Karvonen) zone calculation as a resting-HR proxy for the day of a
    given ECG recording.

    A rolling average rather than the single closest reading, per user
    decision: resting HR in a stable, regularly-training individual moves
    only a few bpm day to day (barring illness/acute stress), so smoothing
    over a week is a reasonable stand-in for a day with no direct morning
    reading at all, without meaningfully sacrificing accuracy. 0 (the
    phone's "omitted means unmeasured" convention, READINESS_SPEC.md) is
    excluded from the average, same as elsewhere. Returns None if there's
    no reading at all in the trailing window — callers must have their own
    fallback for that case (see compute_hr_zone_minutes' docstring)."""
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    window_start = as_of - timedelta(days=6)  # 7-day window, inclusive of as_of

    rows = conn.execute(
        """SELECT resting_hr FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
             AND resting_hr IS NOT NULL AND resting_hr > 0""",
        (window_start.isoformat(), as_of.isoformat()),
    ).fetchall()

    values = [r[0] for r in rows]
    return (sum(values) / len(values)) if values else None


def resting_hr_7day_min(conn: sqlite3.Connection, *, as_of_date: str) -> int | None:
    """7-day trailing MINIMUM of morning resting HR — ANALYSIS_SPEC.md §4.2's
    `hr_rest_for_vo2` input to the VO2max formula specifically calls for a
    minimum, not an average (unlike resting_hr_7day_average above, which
    feeds the unrelated %HRR zone calculation): a resting HR reading can be
    inflated by acute stress/poor sleep/illness on a given morning, and the
    spec's own reasoning is that the *lowest* trailing reading is closer to
    true resting physiology than any single day's number, average included.
    Falls back further to `sessions.resting_hr` over the same window when
    readiness_events has nothing at all in range — same reasoning/fallback
    order as app.ingestion.resting_hr_for_hr_zones (readiness tracking has
    far less history than session syncing does). 0 (the phone's "omitted
    means unmeasured" convention) is excluded from both sources. Returns
    None only if neither source has anything in the trailing week."""
    as_of = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    window_start = as_of - timedelta(days=6)

    readiness_rows = conn.execute(
        """SELECT resting_hr FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
             AND resting_hr IS NOT NULL AND resting_hr > 0""",
        (window_start.isoformat(), as_of.isoformat()),
    ).fetchall()
    values = [r[0] for r in readiness_rows]
    if values:
        return min(values)

    session_rows = conn.execute(
        """SELECT resting_hr FROM sessions
           WHERE date >= ? AND date <= ? AND resting_hr IS NOT NULL AND resting_hr > 0""",
        (window_start.isoformat(), as_of.isoformat()),
    ).fetchall()
    values = [r[0] for r in session_rows]
    return min(values) if values else None


def estimated_vo2max(conn: sqlite3.Connection, *, as_of_date: str) -> float | None:
    """Recomputes VO2max (ANALYSIS_SPEC.md §4.2, Uth-Sørensen-Overgaard-
    Pedersen: `15.3 * hr_max / hr_rest_for_vo2`) server-side, rather than
    trusting sessions.vo2max/readiness_events.vo2max as-is from the phone.

    Two deviations from the phone's own reference formula, both already
    established for the HR-zone/calorie-TRIMP recomputes and reused here
    for consistency rather than maintaining a third, slightly different
    max-HR estimate:
    - `hr_max`: app.ecg_analysis.estimated_max_hr (mean of Fox/Tanaka/
      Gulati) instead of the phone's Tanaka-only formula (ANALYSIS_SPEC.md
      §4.1) — see that function's docstring for why averaging three
      published formulas is preferred over trusting Tanaka's coefficients
      alone.
    - `hr_rest_for_vo2`: resting_hr_7day_min above, which itself falls back
      to sessions.resting_hr when readiness_events has no reading in the
      trailing week — see that function's docstring.

    Returns None if hr_rest_for_vo2 can't be resolved at all (no resting-HR
    reading anywhere in the trailing week, from either source)."""
    hr_rest = resting_hr_7day_min(conn, as_of_date=as_of_date)
    if hr_rest is None or hr_rest <= 0:
        return None

    today_date = datetime.strptime(as_of_date, "%Y-%m-%d").date()
    hr_max = estimated_max_hr(today=today_date)
    return 15.3 * (hr_max / hr_rest)


# How far a scale weigh-in may be from a session's own date and still be
# trusted as "close enough" to that session's actual bodyweight — beyond
# this, body weight itself may have moved enough (illness, water weight,
# a real trend) that using it would risk being a worse input to Keytel than
# just not recomputing at all. Loose enough to cover a user who doesn't
# weigh in every single day (this app's own scale-weighin data shows gaps
# of several days between readings sometimes), tight enough to still mean
# something.
_NEAREST_WEIGHT_MAX_DAYS = 14


def nearest_scale_weight_kg(conn: sqlite3.Connection, *, as_of_date: str) -> float | None:
    """The scale weigh-in closest in calendar time to `as_of_date` (before
    or after — a session and its Keytel/TRIMP recompute cares about "what did
    the user weigh around this time", not strictly "most recent as of this
    session"), feeding app.ecg_analysis.compute_calories_and_trimp as a
    fresher weight input than a static phone-side profile value. Returns
    None if the closest reading is further than _NEAREST_WEIGHT_MAX_DAYS
    away, or if there's no weigh-in at all — callers must leave the
    calorie/TRIMP recompute as "no result" in that case, not guess."""
    row = conn.execute(
        """SELECT weight_kg, ABS(julianday(date) - julianday(?)) AS diff_days
           FROM scale_weighins
           WHERE weight_kg IS NOT NULL AND weight_kg > 0
           ORDER BY diff_days ASC LIMIT 1""",
        (as_of_date,),
    ).fetchone()
    if row is None or row[1] > _NEAREST_WEIGHT_MAX_DAYS:
        return None
    return row[0]


@dataclass
class ReadinessPoint:
    date: str  # calendar date (measured_at truncated to YYYY-MM-DD)
    readiness: str | None  # DELOAD_RECOMMENDED|LIGHT_DAY|NORMAL|GOOD|PEAK|NO_BASELINE
    # Raw numbers behind that day's category — see readiness_daily_trend's
    # docstring for why these are shown as their own trend now (added later
    # than the category plot above) rather than re-deriving/replacing it.
    # 0/0.0 from the phone's own "omitted means unmeasured" convention
    # (READINESS_SPEC.md) is treated as None here, not a real reading —
    # same reasoning as the cardio metrics' positive_only filtering.
    ln_rmssd: float | None
    resting_hr: int | None
    vo2max: float | None


def readiness_daily_trend(
    conn: sqlite3.Connection, *, days: int = 30, today: str | None = None
) -> list[ReadinessPoint]:
    """Daily readiness from readiness_events over the trailing `days` days.

    `readiness` plots the phone's own classification as-is — the phone
    computes this from a rolling baseline it doesn't send us the history
    for, so the server has no way to recompute it; trying to re-derive a
    z-score from server-side history alone was tried and was wrong (too
    little history synced yet to match the phone's baseline). This is a
    category, not a number: DELOAD_RECOMMENDED < LIGHT_DAY < NORMAL < GOOD
    < PEAK — the dashboard plots it on an ordinal axis, not a continuous one.

    `ln_rmssd`/`resting_hr`/`vo2max` are the raw morning readings behind
    that category — unlike re-deriving the category itself, plotting these
    as their own numeric trend doesn't need the phone's baseline history at
    all, and morning/rested HRV is the metric the sports-science literature
    actually treats as reliable (far less noisy than session-time HRV
    during exercise), so it's worth its own chart alongside the category.
    """
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(days=days)

    rows = conn.execute(
        """SELECT measured_at, readiness, ln_rmssd, resting_hr, vo2max
           FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
           ORDER BY measured_at""",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    return [
        ReadinessPoint(
            date=measured_at[:10],
            readiness=readiness,
            ln_rmssd=ln_rmssd or None,
            resting_hr=resting_hr or None,
            vo2max=vo2max or None,
        )
        for measured_at, readiness, ln_rmssd, resting_hr, vo2max in rows
    ]


def readiness_current_week(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[ReadinessPoint]:
    """Readiness for the current calendar week only — Monday through Sunday of
    the ISO week that contains `today` — as exactly 7 points in day order.

    Unlike `readiness_daily_trend` (a 30-day scrolling history feeding the raw
    HR/VO2max charts), this is the small at-a-glance week box: fixed M..S axis,
    one point per weekday. A day with more than one reading keeps only the last
    (max `measured_at`); a day with no reading is a `ReadinessPoint` whose
    fields are all None, so the box always renders all 7 slots.
    """
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    monday = today_date - timedelta(days=today_date.weekday())
    sunday = monday + timedelta(days=6)

    rows = conn.execute(
        """SELECT measured_at, readiness, ln_rmssd, resting_hr, vo2max
           FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
           ORDER BY measured_at""",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchall()

    # ORDER BY measured_at means the last row seen for a given day wins.
    by_day: dict[str, tuple] = {}
    for measured_at, readiness, ln_rmssd, resting_hr, vo2max in rows:
        by_day[measured_at[:10]] = (readiness, ln_rmssd, resting_hr, vo2max)

    week = []
    for i in range(7):
        day = (monday + timedelta(days=i)).isoformat()
        readiness, ln_rmssd, resting_hr, vo2max = by_day.get(day, (None, None, None, None))
        week.append(
            ReadinessPoint(
                date=day,
                readiness=readiness,
                ln_rmssd=ln_rmssd or None,
                resting_hr=resting_hr or None,
                vo2max=vo2max or None,
            )
        )
    return week


def vo2max_4week_trend(conn: sqlite3.Connection, *, today: str | None = None) -> list[WeeklyBucket]:
    """§11.3: 4-week buckets (not weekly) over a 6-month fetch window, bucket
    boundary = window_start + floor(weeks_since_start / 4) * 4 weeks,
    value = median(vo2max where >0).

    Prefers estimated_vo2max (Fox/Tanaka/Gulati hr_max + 7-day min resting
    HR, falling back through sessions.resting_hr — see that function's
    docstring) over the phone's own sessions.vo2max (Tanaka-only hr_max)
    per-session, same "recompute with richer server-side inputs, fall back
    to the phone's value when the recompute can't run" pattern as
    _session_metric_trend's calorie/TRIMP recompute. Computed per-row in
    Python rather than a SQL COALESCE, since estimated_vo2max needs its own
    trailing-window query per date, not a single joinable column."""
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
    for date_str, phone_vo2max in rows:
        vo2max = estimated_vo2max(conn, as_of_date=date_str) or phone_vo2max
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


def steps_avg_per_day_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[WeeklyBucket]:
    """8-week rolling window, median(steps_avg_per_day) per Monday-keyed
    week — same shape as resting_hr_weekly_trend/scale_weekly_trend, sourced
    from readiness_events (SYNC.md "Daily step average") instead of
    sessions/scale_weighins.

    Unlike resting_hr's positive_only filter, 0 is not excluded here — a
    genuinely measured zero-step day is a real (if unusual) value, and
    that's exactly what NULL exists to be distinguished from (see
    READINESS_SPEC.md's field notes: NULL means "no data available", never
    coerced to/from 0). Only NULL rows are dropped; a bucket with no
    non-NULL readings that week is left at value=None by
    _weekly_median_buckets, same "no data this week" semantics as every
    other weekly trend here."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=8)

    rows = conn.execute(
        """SELECT substr(measured_at, 1, 10), steps_avg_per_day FROM readiness_events
           WHERE substr(measured_at, 1, 10) >= ? AND substr(measured_at, 1, 10) <= ?
           ORDER BY measured_at""",
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    filtered = [(d, v) for d, v in rows if v is not None]
    return _weekly_median_buckets(filtered, window_start=window_start, window_end=today_date)


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


def exercise_ids_trained_this_week(
    conn: sqlite3.Connection, *, today: str | None = None
) -> set[str]:
    """Exercise ids with at least one set in the current training week — the
    calendar Monday..Sunday containing `today`, NOT a rolling trailing-7-days
    window (per user decision: "week" means the week-so-far, same Monday-keyed
    convention _monday_of already uses everywhere else in this module, not a
    sliding lookback). Used to scope the Hypertrophy dashboard's
    progressive-overload board to exercises actually trained this week,
    rather than every exercise ever logged."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    week_start = _monday_of(today_date)

    rows = conn.execute(
        """
        SELECT DISTINCT es.exercise_id
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE s.date >= ? AND s.date <= ?
        """,
        (week_start.isoformat(), today_date.isoformat()),
    ).fetchall()
    return {r[0] for r in rows}


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
class BodyweightLoad:
    """The audit trail behind a bodyweight exercise's materialized `weight`,
    from the most recent session that logged it as bodyweight. Purely
    explanatory — nothing else in analysis consumes this (see app.parsing's
    isBodyweight note). `base_weight_kg` is None for a set materialized with
    no scale weigh-in available (its `weight` stayed 0)."""

    load_percent: int | None
    base_weight_kg: float | None
    weight: float
    session_date: str


def latest_bodyweight_load(conn: sqlite3.Connection, exercise_id: str) -> BodyweightLoad | None:
    """Most recent `is_bodyweight=1` set for this exercise, with the
    bw_load_percent / bw_base_weight_kg the phone froze `weight` against.
    Returns None if the exercise has never been logged as bodyweight, or was
    only logged before the phone sent the audit fields (both columns NULL —
    nothing to explain)."""
    row = conn.execute(
        """
        SELECT es.bw_load_percent, es.bw_base_weight_kg, es.weight, s.date
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE es.exercise_id = ? AND es.is_bodyweight = 1
          AND (es.bw_load_percent IS NOT NULL OR es.bw_base_weight_kg IS NOT NULL)
        ORDER BY s.date DESC, es.set_index DESC
        LIMIT 1
        """,
        (exercise_id,),
    ).fetchone()
    if row is None:
        return None
    return BodyweightLoad(
        load_percent=row[0], base_weight_kg=row[1], weight=row[2], session_date=row[3]
    )


@dataclass
class ExerciseSessionPoint:
    session_date: str
    session_id: str
    best_set_weight: float  # heaviest working set that session, 0 if none
    best_e1rm: float | None  # None if no qualifying set (see best_estimated_1rm)
    tonnage: float  # sum(reps*weight) across this session's sets for this exercise


def exercise_history(
    conn: sqlite3.Connection, exercise_id: str, *, weeks: int = 12, today: str | None = None
) -> list[ExerciseSessionPoint]:
    """§1.6-style trend series for a single exercise: one point per session
    that included it, chronological, over a rolling window (12 weeks by
    default, matching ANALYSIS_SPEC.md §1.6's session tonnage/e1RM trend
    window). Not itself in the spec — this is the per-exercise chart the
    dashboard needs, built from the same best_estimated_1rm/set data the
    spec's PR functions already use.

    Weight-bearing rows only: STRETCH sets carry reps/weight NULL (their load
    is a held duration, not a mass — see app.parsing) and CARDIO rows carry
    them NULL too, so both are excluded here rather than crashing the
    reps*weight/`weight > 0` arithmetic below. This mirrors
    muscle_group_weekly_trend, which excludes STRETCH for the same reason —
    an e1RM/tonnage trend is a strength concept."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=weeks)

    rows = conn.execute(
        """
        SELECT s.date, s.id, es.reps, es.weight
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE es.exercise_id = ? AND es.exclude_from_tonnage = 0
          AND es.reps IS NOT NULL AND es.weight IS NOT NULL
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
            tonnage=sum(s.reps * s.weight for s in sets),
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


# ---------------------------------------------------------------------------
# §12 Acute:Chronic Workload Ratio (ACWR) — training-load semaphore
# ---------------------------------------------------------------------------


@dataclass
class AcwrTrend:
    window_end: str  # ISO date
    acute_load: float  # mean total_tonnage, last 7 days
    chronic_load: float  # mean total_tonnage, last 28 days
    acwr: float | None  # None if chronic_load == 0 (not enough data)
    semaphore: str  # GRAY | RED | YELLOW | GREEN


def _load_values_in_window(
    conn: sqlite3.Connection, column: str, start: str, end: str
) -> list[float]:
    """column is one of a fixed internal caller set (total_tonnage,
    session_load) — never user input, so this isn't a string-formatting
    injection risk despite not being a bind parameter."""
    rows = conn.execute(
        f"""
        SELECT {column} FROM sessions  -- noqa: S608
        WHERE date >= ? AND date <= ? AND schema_version > 0
          AND {column} IS NOT NULL AND {column} > 0
        """,
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _tonnage_acwr_semaphore(acwr: float | None) -> str:
    """§12: literature-derived cutoffs (external load — kg lifted)."""
    if acwr is None:
        return "GRAY"
    if 0.8 <= acwr <= 1.3:
        return "GREEN"
    if 0.5 <= acwr < 0.8 or 1.3 < acwr <= 1.5:
        return "YELLOW"
    return "RED"


def _rpe_acwr_semaphore(acwr: float | None) -> str:
    """§13.1: a deliberately wider band than §12's, NOT a literature cutoff —
    nothing validates the tonnage ACWR's numeric thresholds for a subjective
    RPE×duration ratio (see ANALYSIS_SPEC.md §13.1). Widened specifically to
    reflect that lower confidence, so this semaphore reads as a looser
    heuristic rather than borrowing tonnage's bands wholesale."""
    if acwr is None:
        return "GRAY"
    if 0.7 <= acwr <= 1.4:
        return "GREEN"
    if 0.4 <= acwr < 0.7 or 1.4 < acwr <= 1.7:
        return "YELLOW"
    return "RED"


def _compute_acwr_trend(
    conn: sqlite3.Connection,
    column: str,
    semaphore_fn: Callable[[float | None], str],
    *,
    today: str | None = None,
) -> AcwrTrend:
    """Shared implementation behind compute_acwr_trend (tonnage, §12) and
    compute_rpe_acwr_trend (session_load, §13) — same 7-day/28-day rolling
    ratio, just a different source column and semaphore band (see
    _tonnage_acwr_semaphore vs _rpe_acwr_semaphore — deliberately different
    cutoffs, not the same function reused). `today` is an ISO date string
    (YYYY-MM-DD); defaults to the current UTC date. Acute = last 7 days,
    chronic = last 28 days, both inclusive of `today` and rolling (not the
    cardio card's disjoint recent/baseline split)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    acute_start = today_date - timedelta(days=7)
    chronic_start = today_date - timedelta(days=28)

    acute_values = _load_values_in_window(
        conn, column, acute_start.isoformat(), today_date.isoformat()
    )
    chronic_values = _load_values_in_window(
        conn, column, chronic_start.isoformat(), today_date.isoformat()
    )

    acute_load = _mean(acute_values)
    chronic_load = _mean(chronic_values)
    acwr = acute_load / chronic_load if chronic_load > 0 else None

    return AcwrTrend(
        window_end=today_date.isoformat(),
        acute_load=acute_load,
        chronic_load=chronic_load,
        acwr=acwr,
        semaphore=semaphore_fn(acwr),
    )


def compute_acwr_trend(conn: sqlite3.Connection, *, today: str | None = None) -> AcwrTrend:
    """§12: tonnage-based ACWR (the "external load" version)."""
    return _compute_acwr_trend(conn, "total_tonnage", _tonnage_acwr_semaphore, today=today)


# 12-week chart window, matching the existing session tonnage/e1RM trend
# convention (ANALYSIS_SPEC.md §1.6) so this chart spans the same period as
# the other trend charts already on the dashboard.
_ACWR_SERIES_WEEKS = 12


@dataclass
class AcwrPoint:
    date: str  # ISO date
    acute_load: float
    chronic_load: float
    acwr: float | None
    semaphore: str


def _acwr_daily_trend(
    conn: sqlite3.Connection,
    column: str,
    semaphore_fn: Callable[[float | None], str],
    *,
    today: str | None = None,
) -> list[AcwrPoint]:
    """Shared implementation behind acwr_daily_trend (tonnage, §12) and
    rpe_acwr_daily_trend (session_load, §13). One AcwrPoint per Monday-keyed
    week over a 12-week window (same weekly cadence as the other trend
    charts, e.g. resting_hr_weekly_trend) — each point is still the same
    7-day-acute/28-day-chronic ratio evaluated *as of* that Monday, just
    sampled weekly instead of daily. A daily-resolution ACWR line is noisier
    than the "this week vs last month" framing in the chart's own
    description warrants; the underlying ratio and windows are unchanged,
    only the sampling cadence is coarser."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    series_start = today_date - timedelta(weeks=_ACWR_SERIES_WEEKS)
    fetch_start = series_start - timedelta(days=28)  # cover the earliest week's chronic lookback

    # (date, value) pairs, one per qualifying session, ordered by date —
    # matches _load_values_in_window's per-session values (a day with
    # multiple sessions contributes each one separately to the mean).
    rows: list[tuple[str, float]] = conn.execute(
        f"""
        SELECT date, {column} FROM sessions  -- noqa: S608 — column from a fixed internal caller set
        WHERE date >= ? AND date <= ? AND schema_version > 0
          AND {column} IS NOT NULL AND {column} > 0
        ORDER BY date
        """,
        (fetch_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    points: list[AcwrPoint] = []
    cursor = _monday_of(series_start)
    while cursor <= today_date:
        acute_start = cursor - timedelta(days=7)
        chronic_start = cursor - timedelta(days=28)

        acute_values = [v for d, v in rows if acute_start.isoformat() <= d <= cursor.isoformat()]
        chronic_values = [
            v for d, v in rows if chronic_start.isoformat() <= d <= cursor.isoformat()
        ]

        acute_load = _mean(acute_values)
        chronic_load = _mean(chronic_values)
        acwr = acute_load / chronic_load if chronic_load > 0 else None

        points.append(
            AcwrPoint(
                date=cursor.isoformat(),
                acute_load=acute_load,
                chronic_load=chronic_load,
                acwr=acwr,
                semaphore=semaphore_fn(acwr),
            )
        )
        cursor += timedelta(days=7)

    return points


def acwr_daily_trend(conn: sqlite3.Connection, *, today: str | None = None) -> list[AcwrPoint]:
    """§12: tonnage-based ACWR chart series (the "external load" version)."""
    return _acwr_daily_trend(conn, "total_tonnage", _tonnage_acwr_semaphore, today=today)


# ---------------------------------------------------------------------------
# §13 Session-RPE (Foster method) — subjective training load
# ---------------------------------------------------------------------------

# Same 12-week window as the ACWR/tonnage/e1RM trend charts, so every
# training-load chart on the dashboard spans a consistent period.
_SESSION_LOAD_SERIES_WEEKS = 12


@dataclass
class SessionLoadPoint:
    date: str  # ISO date
    session_rpe: int | None
    session_load: float | None


def session_load_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[SessionLoadPoint]:
    """§13 chart series: one point per session with a recorded RPE, over a
    12-week window. Sessions where the user skipped the RPE prompt are
    omitted entirely (not plotted as 0 or null-filled) rather than
    interpolated — an unanswered rating carries no information to show."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_SESSION_LOAD_SERIES_WEEKS)

    rows = conn.execute(
        """
        SELECT date, session_rpe, session_load FROM sessions
        WHERE date >= ? AND date <= ? AND schema_version > 0
          AND session_rpe IS NOT NULL
        ORDER BY date
        """,
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    return [
        SessionLoadPoint(date=date_str, session_rpe=rpe, session_load=load)
        for date_str, rpe, load in rows
    ]


@dataclass
class SessionMetricPoint:
    date: str  # ISO date
    value: float


def _session_metric_trend(
    conn: sqlite3.Connection, column: str, *, ecg_column: str, today: str | None = None
) -> list[SessionMetricPoint]:
    """Shared shape for a plain per-session numeric trend: one point per
    session, over the same 12-week window as the other training-load/cardio
    charts. Rows where the field was omitted (parsed as 0, per the sender's
    space-saving convention — see docs/sync-ingestion/SPEC.md) are skipped
    rather than plotted as 0, same reasoning as vo2max_4week_trend's `> 0`
    filter.

    Prefers `ecg_analysis.{ecg_column}` (app.ecg_analysis.
    compute_calories_and_trimp — real beat-by-beat ECG HR + nearest scale
    weight, computed server-side) over `sessions.{column}` (the phone's own
    incremental, optical-sensor-HR value) whenever the ECG recompute exists
    for that session — COALESCE, not a WHERE filter, so a session without an
    ECG recording still falls back to the phone's own number rather than
    disappearing from the chart entirely."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_SESSION_LOAD_SERIES_WEEKS)

    rows = conn.execute(
        f"""
        SELECT s.date, COALESCE(e.{ecg_column}, s.{column}) AS value
        FROM sessions s
        LEFT JOIN ecg_analysis e ON e.session_id = s.id
        WHERE s.date >= ? AND s.date <= ? AND s.schema_version > 0
          AND COALESCE(e.{ecg_column}, s.{column}) > 0
        ORDER BY s.date
        """,  # noqa: S608
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    return [SessionMetricPoint(date=date_str, value=value) for date_str, value in rows]


def session_calories_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[SessionMetricPoint]:
    """Per-session calories burned, plotted alongside tonnage/session-load
    in the Training load section. Prefers the ECG-based recompute over the
    phone's own value — see _session_metric_trend's docstring."""
    return _session_metric_trend(
        conn, "session_calories", ecg_column="session_calories_ecg", today=today
    )


def session_trimp_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> list[SessionMetricPoint]:
    """Per-session TRIMP (Banister training impulse), plotted in the
    Recovery & fitness trends section alongside the other per-session
    cardio load metrics. Prefers the ECG-based recompute over the phone's
    own value — see _session_metric_trend's docstring."""
    return _session_metric_trend(conn, "session_trimp", ecg_column="session_trimp_ecg", today=today)


# ACWR applied to session_load instead of total_tonnage — the standard way
# session-RPE is actually used for injury/overtraining monitoring in the
# literature (an absolute RPE×duration number has no meaning on its own;
# the acute:chronic ratio is what's normally tracked). A second, separate
# chart from the raw session_load_trend above, per user decision — not a
# replacement for it, since the raw per-session ratings are still useful to
# see on their own.
def compute_rpe_acwr_trend(conn: sqlite3.Connection, *, today: str | None = None) -> AcwrTrend:
    """§13.1: session-load-based ACWR (the "internal load" version of §12).
    Uses _rpe_acwr_semaphore's wider, non-literature band — see that
    function's docstring and ANALYSIS_SPEC.md §13.1 for why it isn't
    _tonnage_acwr_semaphore's cutoffs reused."""
    return _compute_acwr_trend(conn, "session_load", _rpe_acwr_semaphore, today=today)


def rpe_acwr_daily_trend(conn: sqlite3.Connection, *, today: str | None = None) -> list[AcwrPoint]:
    """§13.1 chart series: session-load-based ACWR, same shape as acwr_daily_trend."""
    return _acwr_daily_trend(conn, "session_load", _rpe_acwr_semaphore, today=today)


# ---------------------------------------------------------------------------
# §14 Training monotony & strain (Foster et al. 1998) — NOT in ANALYSIS_SPEC.md,
# added 2026-08 as a second training-load lens alongside §12/§13's ACWR, on the
# same session_load (RPE × duration) input already collected for §13.
#
# monotony = mean(daily_load) / stdev(daily_load) over a 7-day window — high
# monotony means every day carries a similar load (little easy/hard variation),
# which Foster's original study (collegiate swimmers) found associated with a
# higher rate of illness/injury/overreaching than the same weekly load spread
# unevenly across hard and easy days.
# strain = sum(daily_load) * monotony — a single number meant to be tracked
# against the athlete's OWN history over time, not compared to a population
# cutoff; the literature does not establish one (see below).
#
# Days with no session are treated as load=0 for both the mean/stdev (a rest
# day is real information about variation, not a missing data point to skip —
# omitting rest days would make monotony compute "average hard-day load
# smoothness" instead of "how varied was this week", which is a different and
# less useful quantity for exactly what this metric is meant to catch: a
# string of same-intensity days with no easy day mixed in).
#
# Threshold honesty (checked before implementing, per this project's own
# standard of not inventing cutoffs — see _rpe_acwr_semaphore's docstring for
# the precedent): monotony > 2.0 is the one value that recurs across secondary
# literature (e.g. Gabbett 2016's training-load review) as an "attention"
# zone, but it traces back to a single observational study on a specific
# athlete population (collegiate swimmers), not an independently validated
# clinical cutoff — same epistemic tier as this project's own ACWR bands, one
# rung below something like HRR60s's Cole et al. mortality-risk cutoff. Shown
# as a labeled reference line, not a RED/YELLOW/GREEN semaphore. Strain has no
# recurring absolute reference at all in the literature (it's discussed as
# "compare against your own recent weeks", not a population number) — plotted
# as a bare trend, no reference line, same "don't invent a threshold" posture
# already applied to e.g. afibEpisodes/pauses/premature/uneven in the
# dashboard (see dashboard.html's THRESHOLDS comment).
# ---------------------------------------------------------------------------

_MONOTONY_WINDOW_DAYS = 7
_MONOTONY_ATTENTION_THRESHOLD = 2.0  # Foster et al. 1998 — see docstring above for caveats


@dataclass
class MonotonyPoint:
    week_end: str  # ISO date, last day of the 7-day window this point summarizes
    monotony: float | None  # None if stdev is 0 (all-equal or single-day load) or no load data
    strain: float | None
    daily_loads: list[float]  # 7 values, oldest first, 0.0 for rest/no-RPE days


def _daily_session_loads(conn: sqlite3.Connection, *, window_start, window_end) -> dict[str, float]:
    """Sum of session_load per calendar date within the window (a day could
    in principle have more than one logged session). Days with no row default
    to 0.0 by the caller (_compute_monotony below), not here — this only
    returns what's actually present."""
    rows = conn.execute(
        """
        SELECT date, SUM(session_load) FROM sessions
        WHERE date >= ? AND date <= ? AND schema_version > 0
          AND session_load IS NOT NULL
        GROUP BY date
        """,
        (window_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    return {date_str: total for date_str, total in rows}


def _compute_monotony(daily_loads: list[float]) -> tuple[float | None, float | None]:
    """daily_loads: 7 values, rest days already zero-filled by the caller.
    Population stdev (like §5.3's HRV baseline z-score), not sample stdev —
    this is a full description of the observed week, not an estimate of a
    larger population's spread."""
    n = len(daily_loads)
    mean_load = sum(daily_loads) / n
    variance = sum((x - mean_load) ** 2 for x in daily_loads) / n
    stdev = variance**0.5
    if stdev == 0:
        return None, None  # flat week (all-zero or all-identical load) — ratio undefined
    monotony = mean_load / stdev
    strain = sum(daily_loads) * monotony
    return monotony, strain


def training_monotony(conn: sqlite3.Connection, *, today: str | None = None) -> MonotonyPoint:
    """Current 7-day monotony/strain — the single-value card, same shape as
    compute_acwr_trend's "as of today" evaluation."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(days=_MONOTONY_WINDOW_DAYS - 1)
    loads_by_date = _daily_session_loads(conn, window_start=window_start, window_end=today_date)

    daily_loads = [
        loads_by_date.get((window_start + timedelta(days=i)).isoformat(), 0.0)
        for i in range(_MONOTONY_WINDOW_DAYS)
    ]
    monotony, strain = _compute_monotony(daily_loads)
    return MonotonyPoint(
        week_end=today_date.isoformat(), monotony=monotony, strain=strain, daily_loads=daily_loads
    )


def training_monotony_weekly_trend(
    conn: sqlite3.Connection, *, weeks: int = 12, today: str | None = None
) -> list[MonotonyPoint]:
    """Chart series: one point per Monday-keyed week (same cadence as the
    other weekly trend charts — resting_hr_weekly_trend, ACWR's weekly
    sampling, etc.), each the full trailing-7-day monotony/strain evaluated
    as of that week's Sunday (its last day), not a daily-resolution series —
    same "coarser cadence reads less noisy" reasoning already applied to
    ACWR's chart sampling (see compute_acwr_trend's docstring)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    # Anchor on Mondays like every other weekly chart, but evaluate each
    # window ending on that week's Sunday (start-of-week + 6 days) so the
    # 7-day trailing window aligns with a full calendar week rather than
    # spilling into the next one.
    first_monday = _monday_of(today_date) - timedelta(weeks=weeks - 1)

    points = []
    for i in range(weeks):
        week_start = first_monday + timedelta(weeks=i)
        week_end = min(week_start + timedelta(days=6), today_date)
        if week_end < week_start:
            continue
        loads_by_date = _daily_session_loads(conn, window_start=week_start, window_end=week_end)
        span_days = (week_end - week_start).days + 1
        daily_loads = [
            loads_by_date.get((week_start + timedelta(days=i)).isoformat(), 0.0)
            for i in range(span_days)
        ]
        monotony, strain = _compute_monotony(daily_loads)
        points.append(
            MonotonyPoint(
                week_end=week_end.isoformat(),
                monotony=monotony,
                strain=strain,
                daily_loads=daily_loads,
            )
        )
    return points


# ---------------------------------------------------------------------------
# Estimated daily maintenance calories — NOT in ANALYSIS_SPEC.md, added on
# user request. A rough, formula-only TDEE estimate for deciding a diet
# starting point (cut/bulk), *not* derived from the weight trend itself —
# weight is the thing the user will move deliberately in response to this
# number, so it can't also be an input to computing it (that would be
# circular: the estimate would just chase whatever the diet already did).
# ---------------------------------------------------------------------------

# Deliberately wide — a priori BMR+NEAT+EAT+TEF stacking has no empirical
# anchor (unlike a weight-trend-calibrated TDEE), so the honest uncertainty
# band is wider than the trend charts elsewhere in this module. See the
# chat discussion this was added from: expect roughly ±15-20% real-world
# accuracy from formulas alone.
_MAINTENANCE_RANGE_FRACTION = 0.15

# Rough published estimate (~0.04-0.05 kcal per kg body weight per step);
# reference-only constant, not a citation-grade figure — steps are a NEAT
# proxy here, not a precise pedometer-calorie formula.
_KCAL_PER_STEP_PER_KG = 0.0005

# Thermic effect of food as a fixed fraction of BMR+NEAT+EAT, since actual
# food intake isn't tracked by this server at all (see PENDING_IMPROVEMENTS.md
# for other data gaps) — a fixed +10% stand-in is standard practice when
# intake is unknown.
_TEF_FRACTION = 0.10


@dataclass
class MaintenanceEstimate:
    low: float
    mid: float
    high: float
    bmr: float
    bmr_formula: str  # "katch-mcardle" or "mifflin-st-jeor" — see estimated_maintenance_calories
    bmr_inputs: str  # human-readable "(...)" of the inputs bmr_formula actually used, for display
    neat: float
    eat: float
    tef: float
    weight_kg: float
    steps_avg_per_day: float | None
    session_calories_per_day: float


def estimated_maintenance_calories(
    conn: sqlite3.Connection, *, today: str | None = None
) -> MaintenanceEstimate | None:
    """A priori daily maintenance (TDEE) estimate: BMR (see below) +
    step-based NEAT + averaged-out session EAT + fixed-fraction TEF, given
    as a range rather than a point value (see _MAINTENANCE_RANGE_FRACTION).

    Deliberately does NOT use the scale weight *trend* or any other
    feedback signal — see this function's module-level comment block for
    why. Only the most recent single weight/body-composition reading is
    used, as a BMR input, same as any other "current state" input
    (height/age/sex).

    BMR formula — prefers real measured body composition over assumed
    profile constants:
    - Katch-McArdle (`370 + 21.6 * lean_mass_kg`) whenever the latest
      weigh-in has a real BIA reading (`body_fat_percent > 0`, same
      "real reading" convention as scale_weekly_trend's leanMassPercent
      bucket) — lean mass is a better BMR predictor than height/age/sex
      per the research this was added from (Mifflin-St Jeor's own inputs
      are a weaker proxy for the same thing Katch-McArdle measures
      directly). VO2max/HRV were considered too (both readily available
      here from readiness_events) but the literature shows both lose
      their predictive power once you control for lean mass, so neither
      is used as a BMR input.
    - Mifflin-St Jeor fallback (original formula) when there's a weight
      but no real BIA reading yet (bodyFatPercent<=0/missing) — same
      ASSUMED_HEIGHT_CM/ASSUMED_SEX/ASSUMED_BIRTH_YEAR placeholders as
      before.

    Returns None if there's no weight reading to anchor the BMR term —
    every other input degrades gracefully (missing steps -> neat=0, no
    recent sessions -> eat=0), but BMR has no reasonable default and
    without it the whole estimate is meaningless.
    """
    from app.config import ASSUMED_BIRTH_YEAR, ASSUMED_HEIGHT_CM, ASSUMED_SEX

    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()

    weight_row = conn.execute(
        "SELECT weight_kg, body_fat_percent, lean_mass_percent FROM scale_weighins "
        "WHERE weight_kg IS NOT NULL AND date <= ? ORDER BY date DESC LIMIT 1",
        (today_date.isoformat(),),
    ).fetchone()
    if weight_row is None:
        return None
    weight_kg, body_fat_percent, lean_mass_percent = weight_row

    if body_fat_percent is not None and body_fat_percent > 0 and lean_mass_percent is not None:
        lean_mass_kg = weight_kg * (lean_mass_percent / 100.0)
        bmr = 370 + 21.6 * lean_mass_kg
        bmr_formula = "katch-mcardle"
        bmr_inputs = f"lean mass {lean_mass_kg:.1f}kg"
    else:
        age_years = today_date.year - ASSUMED_BIRTH_YEAR
        sex_constant = 5 if ASSUMED_SEX == "male" else -161
        bmr = 10 * weight_kg + 6.25 * ASSUMED_HEIGHT_CM - 5 * age_years + sex_constant
        bmr_formula = "mifflin-st-jeor"
        bmr_inputs = f"{weight_kg:.1f}kg, {ASSUMED_HEIGHT_CM}cm, {age_years}y, {ASSUMED_SEX}"

    steps_row = conn.execute(
        "SELECT steps_avg_per_day FROM readiness_events WHERE steps_avg_per_day IS NOT NULL "
        "AND substr(measured_at, 1, 10) <= ? ORDER BY measured_at DESC LIMIT 1",
        (today_date.isoformat(),),
    ).fetchone()
    steps_avg_per_day = steps_row[0] if steps_row else None
    neat = (steps_avg_per_day or 0.0) * _KCAL_PER_STEP_PER_KG * weight_kg

    # Same "prefer ECG recompute, fall back to phone's own value" pattern as
    # _session_metric_trend, averaged over the trailing week and spread
    # across all 7 days (not just training days) to get a daily rate.
    window_start = today_date - timedelta(days=7)
    calorie_rows = conn.execute(
        """
        SELECT COALESCE(e.session_calories_ecg, s.session_calories) AS value
        FROM sessions s
        LEFT JOIN ecg_analysis e ON e.session_id = s.id
        WHERE s.date > ? AND s.date <= ? AND s.schema_version > 0
          AND COALESCE(e.session_calories_ecg, s.session_calories) > 0
        """,  # noqa: S608
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()
    eat = sum(value for (value,) in calorie_rows) / 7.0

    tef = (bmr + neat + eat) * _TEF_FRACTION
    mid = bmr + neat + eat + tef

    return MaintenanceEstimate(
        low=mid * (1 - _MAINTENANCE_RANGE_FRACTION),
        mid=mid,
        high=mid * (1 + _MAINTENANCE_RANGE_FRACTION),
        bmr=bmr,
        bmr_formula=bmr_formula,
        bmr_inputs=bmr_inputs,
        neat=neat,
        eat=eat,
        tef=tef,
        weight_kg=weight_kg,
        steps_avg_per_day=steps_avg_per_day,
        session_calories_per_day=eat,
    )


# ---------------------------------------------------------------------------
# Weekly hypertrophy volume/frequency per muscle group — NOT in
# ANALYSIS_SPEC.md, added on user request. `exercise_sets.bodypart` is
# already synced and parsed (see SPEC.md's example payload and
# app.parsing) but was previously only consumed pre-aggregated as
# tonnage_by_bodypart; these two functions are the first things to read it
# directly for a per-set, per-week count. No app change needed.
#
# Thresholds are Schoenfeld et al. 2017 (meta-analysis of 15 RCTs) and the
# broader "sets per muscle per week" literature it anchored: <5 sets/week is
# sub-optimal, 5-9 is a moderate response, 10-20 is the well-supported
# "optimal" band across trained-population studies, and >20 is where
# evidence gets thin and recovery cost starts working against the user
# rather than for them. Deliberately a single fixed band, not tiered by
# experience level, per user decision — this repo is single-user, so a
# configurable "experience level" setting would be state serving no one.
# ---------------------------------------------------------------------------


def _volume_semaphore(set_count: int) -> str:
    if set_count < 5:
        return "RED"
    if set_count < 10:
        return "YELLOW"
    if set_count <= 20:
        return "GREEN"
    return "YELLOW"  # >20/week: evidence thins out, recovery cost risk


def _frequency_semaphore(session_count: int) -> str:
    """Training a muscle group 2+ times/week outperforms 1x/week at equal
    weekly volume in the frequency literature (e.g. the studies underlying
    Weightology's frequency review) — 0 sessions means it wasn't trained at
    all that week, which is a real gap, not neutral."""
    if session_count <= 0:
        return "RED"
    if session_count == 1:
        return "YELLOW"
    return "GREEN"


@dataclass
class MuscleGroupWeek:
    week_start: str  # ISO date of the Monday keying this bucket
    bodypart: str
    set_count: int  # completed sets (reps>0, weight>0 or bodyweight), primary bodypart only
    session_count: int  # distinct sessions that week touching this bodypart
    volume_semaphore: str  # GRAY | RED | YELLOW | GREEN
    frequency_semaphore: str  # RED | YELLOW | GREEN


# Same 12-week convention as the other trend charts (ANALYSIS_SPEC.md
# §1.6), so this sits alongside them on a dashboard spanning a consistent
# period.
_MUSCLE_GROUP_SERIES_WEEKS = 12


def muscle_group_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[MuscleGroupWeek]]:
    """Weekly working-set count and session frequency per bodypart, keyed by
    bodypart then Monday-keyed week — the missing "is this actually enough
    volume for hypertrophy" view that total_tonnage/tonnage_by_bodypart
    alone don't answer (a heavy single set and ten light ones can produce
    the same tonnage but very different set-count-driven hypertrophy
    stimulus per the volume literature — see _volume_semaphore).

    Only counts a set on its primary bodypart, as synced from the phone —
    no secondary/synergist-muscle weighting, per user decision (the primary
    bodypart is already clean, unambiguous data; a synergist-weighting
    table would need to be maintained by hand and would encode arbitrary
    judgment calls about fractional credit).

    A "working set" here is (reps>0 and weight>0) or (reps>0 and
    is_bodyweight) — same completed-set definition as _is_untouched_or_empty
    elsewhere in this module, so this doesn't count warmups/untouched/
    bodyweight-mistaken-for-empty sets as volume. `exclude_from_tonnage` is
    deliberately NOT applied: per user decision a daily/accessory set the
    phone keeps out of the *session tonnage* is still hypertrophy volume for
    the muscle it trained — the flag is a load-accounting choice, not a
    "this set wasn't work" marker (same reasoning as stretch_weekly_trend).
    STRETCH-type exercises are excluded (bodypart-set volume is a
    hypertrophy/strength concept; stretching isn't working sets toward it).

    Matches `exercise_type` case-insensitively: SPEC.md's documented values
    are `FORZA`/`STRETCH`, but real synced data observed in this DB has
    lowercase `forza`/`stretch` — same discrepancy already latent in
    app.parsing's `is_cardio = exercise_type == "CARDIO"` check. Rather than
    silently drift into the same trap (a filter that never matches), this
    query is written to work regardless of which casing the sender actually
    uses; see the CLAUDE.md-flagged conversation this was found in for the
    wider fix that may still be needed in app.parsing.
    """
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_MUSCLE_GROUP_SERIES_WEEKS)

    rows = conn.execute(
        """
        SELECT s.date, s.id, es.bodypart, es.reps, es.weight, es.is_bodyweight
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE s.date >= ? AND s.date <= ? AND s.schema_version > 0
          AND UPPER(es.exercise_type) = 'FORZA'
          AND es.bodypart IS NOT NULL AND es.bodypart != ''
          AND es.reps > 0 AND (es.weight > 0 OR es.is_bodyweight = 1)
        ORDER BY s.date
        """,
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    week_cursor = _monday_of(window_start)
    week_starts: list[str] = []
    while week_cursor <= today_date:
        week_starts.append(week_cursor.isoformat())
        week_cursor += timedelta(days=7)

    # bodypart -> week_start -> {"sets": int, "sessions": set of session_id}
    by_bodypart: dict[str, dict[str, dict]] = {}
    for date_str, session_id, bodypart, _reps, _weight, _is_bw in rows:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        week_key = _monday_of(d).isoformat()
        if week_key not in week_starts:
            continue
        bucket = by_bodypart.setdefault(
            bodypart, {w: {"sets": 0, "sessions": set()} for w in week_starts}
        )
        bucket[week_key]["sets"] += 1
        bucket[week_key]["sessions"].add(session_id)

    result: dict[str, list[MuscleGroupWeek]] = {}
    for bodypart, weeks in sorted(by_bodypart.items()):
        result[bodypart] = [
            MuscleGroupWeek(
                week_start=week,
                bodypart=bodypart,
                set_count=weeks[week]["sets"],
                session_count=len(weeks[week]["sessions"]),
                volume_semaphore=_volume_semaphore(weeks[week]["sets"]),
                frequency_semaphore=_frequency_semaphore(len(weeks[week]["sessions"])),
            )
            for week in week_starts
        ]
    return result


# ---------------------------------------------------------------------------
# Weekly stretching volume per muscle group — the STRETCH-type counterpart to
# muscle_group_weekly_trend above, added on user request. Not in
# ANALYSIS_SPEC.md.
#
# Threshold is Ingram et al. 2025 (Sports Medicine 55(3):597-617), a
# meta-analysis + multivariate meta-regression of 189 studies / 6,654 adults
# — "improvements in flexibility were maximised by a cumulative stretching
# volume of ... 10 min per week (chronic)", with "no notable increases in
# flexibility beyond 10 min of static stretching per week" and no lower
# plateau (more is better up to 10 min). Supporting: Palma et al. (via the
# Huberman Lab newsletter) put the effective minimum at ~5 min/muscle/week;
# Medeiros & Martini found >5 min/week beats <5 min/week for ROM.
#
# The band here (RED <5 min, GREEN 5-10 min, RED >10 min) is per user
# decision. The >10-min RED is NOT from the literature — Ingram found no harm
# from over-stretching, only a plateau — it's a deliberate "time better spent
# on an under-stretched muscle" signal in this single-user tool.
#
# Ingram also found weekly *frequency* had no independent effect on
# flexibility (g = -0.001, p = 0.95: "5 min in one session was as good as
# spread over 5 days"), so unlike muscle_group_weekly_trend there is
# deliberately no frequency semaphore here — total weekly held-seconds is the
# only lever the evidence supports.
# ---------------------------------------------------------------------------

_STRETCH_WEEKLY_RED_BELOW_SEC = 5 * 60  # < 5 min/week: below the effective dose
_STRETCH_WEEKLY_RED_ABOVE_SEC = 10 * 60  # > 10 min/week: plateau, time better spent elsewhere


def _stretch_volume_semaphore(total_seconds: int) -> str:
    if total_seconds < _STRETCH_WEEKLY_RED_BELOW_SEC:
        return "RED"
    if total_seconds <= _STRETCH_WEEKLY_RED_ABOVE_SEC:
        return "GREEN"
    return "RED"


@dataclass
class StretchMuscleGroupWeek:
    week_start: str  # ISO date of the Monday keying this bucket
    bodypart: str
    total_seconds: int  # summed timeSeconds of stretch sets on this bodypart that week
    session_count: int  # distinct sessions that week that stretched this bodypart
    volume_semaphore: str  # RED | GREEN (see _stretch_volume_semaphore)


def stretch_weekly_trend(
    conn: sqlite3.Connection, *, today: str | None = None
) -> dict[str, list[StretchMuscleGroupWeek]]:
    """Weekly held-seconds of STRETCH-type work per bodypart, keyed by
    bodypart then Monday-keyed week — the flexibility-dose counterpart to
    muscle_group_weekly_trend (which deliberately excludes STRETCH as it's a
    hypertrophy/strength view). See this section's module comment for the
    Ingram et al. 2025 threshold.

    A stretch set counts only if it carries a positive `time_seconds` (a
    real held duration synced from the phone's `{timeSeconds, done}`);
    untouched/abandoned sets with NULL time_seconds are skipped, same
    completed-set spirit as muscle_group_weekly_trend's reps>0 filter.
    `exclude_from_tonnage` is not applied here — it's a tonnage-accounting
    flag with no meaning for stretch volume.

    Same case-insensitive `exercise_type` match and 12-week window as
    muscle_group_weekly_trend, for the same reasons (real synced data sends
    lowercase `stretch`; a consistent dashboard period)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    window_start = today_date - timedelta(weeks=_MUSCLE_GROUP_SERIES_WEEKS)

    rows = conn.execute(
        """
        SELECT s.date, s.id, es.bodypart, es.time_seconds
        FROM exercise_sets es
        JOIN sessions s ON s.id = es.session_id
        WHERE s.date >= ? AND s.date <= ? AND s.schema_version > 0
          AND UPPER(es.exercise_type) = 'STRETCH'
          AND es.bodypart IS NOT NULL AND es.bodypart != ''
          AND es.time_seconds IS NOT NULL AND es.time_seconds > 0
        ORDER BY s.date
        """,
        (window_start.isoformat(), today_date.isoformat()),
    ).fetchall()

    week_cursor = _monday_of(window_start)
    week_starts: list[str] = []
    while week_cursor <= today_date:
        week_starts.append(week_cursor.isoformat())
        week_cursor += timedelta(days=7)

    # bodypart -> week_start -> {"seconds": int, "sessions": set of session_id}
    by_bodypart: dict[str, dict[str, dict]] = {}
    for date_str, session_id, bodypart, time_seconds in rows:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        week_key = _monday_of(d).isoformat()
        if week_key not in week_starts:
            continue
        bucket = by_bodypart.setdefault(
            bodypart, {w: {"seconds": 0, "sessions": set()} for w in week_starts}
        )
        bucket[week_key]["seconds"] += time_seconds
        bucket[week_key]["sessions"].add(session_id)

    result: dict[str, list[StretchMuscleGroupWeek]] = {}
    for bodypart, weeks in sorted(by_bodypart.items()):
        result[bodypart] = [
            StretchMuscleGroupWeek(
                week_start=week,
                bodypart=bodypart,
                total_seconds=weeks[week]["seconds"],
                session_count=len(weeks[week]["sessions"]),
                volume_semaphore=_stretch_volume_semaphore(weeks[week]["seconds"]),
            )
            for week in week_starts
        ]
    return result


# ---------------------------------------------------------------------------
# Progressive-overload verdict per exercise — NOT in ANALYSIS_SPEC.md, added
# on user request. Builds on exercise_history (already computes the raw
# e1RM/tonnage/session points); this adds an explicit judgment so the
# dashboard doesn't require eyeballing a chart to answer "is this exercise
# actually progressing". Computed independently for e1RM (closer to max
# strength — sensitive to load at low reps, largely insensitive to how much
# volume was done) and tonnage (reps*weight — closer to the hypertrophy
# volume literature, sensitive to set/rep count, not just top load); the two
# can disagree (e.g. more sets at the same weight raises tonnage without
# moving e1RM at all), which is itself informative rather than a bug to
# collapse into one number.
#
# Compared week-to-week (average of the current Monday..today week vs the
# average of the last non-empty PRIOR week — not the immediately preceding
# calendar week, which may have no sessions for this exercise at all, and
# not session-to-session), per user decision after discussing the
# alternatives: session-to-session is too noisy for an exercise sometimes
# trained "primary" (heavy, near-limit) and sometimes "secondary"/accessory
# (lighter, higher RIR) in different sessions — the same noise problem the
# periodization literature's own "gradual load increases of 2-5%/week over
# an 8-week period" framing addresses by describing weekly, not per-session,
# progression. Averaging within a week (rather than taking that week's best
# single value) absorbs a mixed primary/secondary week into one representative
# number instead of letting one heavy outlier session define the week.
# ---------------------------------------------------------------------------

# A week needs at least this many qualifying points to count as "real data"
# for that week's average, not a single noisy set standing in for the whole
# week.
_OVERLOAD_MIN_POINTS_PER_WEEK = 1

# No published literature gives a precise week-over-week % threshold for
# "growing too slowly" / "growing too fast" on a single lift's e1RM or
# tonnage — periodization research (Stronger by Science's load-progression
# guidance: "gradual load increases of 2-5% per week... over an 8-week
# period", powerlifting periodization studies) only supports *rough*,
# heavily level-dependent bands: novices can add load rapidly (sometimes
# several % per session for a few blocks), intermediates progress on the
# order of low-single-digit % per week before it tapers off, advanced
# lifters much less. Rather than invent one arbitrary number, this uses a
# single deliberately wide "plausible progression" band loosely anchored to
# that 2-5%/week intermediate reference (the middle of the broader spread,
# and this is a single-user tool with no stored experience-level setting to
# pick a different one) — RATE_TOO_FAST exists mainly to flag a likely
# data/warmup artifact (e.g. a warmup set not excluded from tonnage) rather
# than to claim "you grew unhealthily fast", which strength/tonnage metrics
# have no real basis to diagnose. Explicitly labeled a rough reference, not
# a clinical or literature-exact cutoff, same honesty standard as
# estimated_maintenance_calories' BMR+NEAT+EAT stacking.
_OVERLOAD_RATE_STALLED_PCT_PER_WEEK = 0.1  # below this: essentially flat
_OVERLOAD_RATE_TOO_FAST_PCT_PER_WEEK = 5.0  # above this: implausible for an established lift


@dataclass
class OverloadMetricVerdict:
    verdict: str  # UP_HEALTHY | UP_TOO_FAST | STALLED | DOWN | INSUFFICIENT_DATA
    current_week_avg: float | None
    prior_week_avg: float | None
    prior_week_start: str | None  # ISO date (Monday) of the prior week actually compared against
    weeks_ago: int | None  # how many calendar weeks back that prior week is (>=1)
    change_percent: float | None  # this-vs-prior-week change, already a weekly rate
    percent_per_week: float | None  # kept as an explicit alias of change_percent for now


@dataclass
class ProgressiveOverloadVerdict:
    exercise_id: str
    point_count: int
    e1rm: OverloadMetricVerdict
    tonnage: OverloadMetricVerdict


def _weekly_averages(
    points: list[tuple[str, float]],
    *,
    current_week_start,
) -> dict:
    """Groups (date, value) points into Monday-keyed weekly averages —
    shared helper for both e1RM and tonnage series. Returns
    {week_start_iso: average_value}."""
    by_week: dict[str, list[float]] = {}
    for date_str, value in points:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        week_key = _monday_of(d).isoformat()
        by_week.setdefault(week_key, []).append(value)
    return {
        week: _mean(values)
        for week, values in by_week.items()
        if len(values) >= _OVERLOAD_MIN_POINTS_PER_WEEK
    }


def _overload_metric_verdict(
    points: list[tuple[str, float]], *, current_week_start
) -> OverloadMetricVerdict:
    """This-week-vs-last-non-empty-prior-week verdict, applied identically to
    e1RM and tonnage series. `points` are (session_date, value) pairs across
    the full lookback window; `current_week_start` is the Monday of the
    week being evaluated (normally "this week", i.e. today's Monday)."""
    weekly_avg = _weekly_averages(points, current_week_start=current_week_start)
    current_key = current_week_start.isoformat()

    if current_key not in weekly_avg:
        return OverloadMetricVerdict(
            verdict="INSUFFICIENT_DATA",
            current_week_avg=None,
            prior_week_avg=None,
            prior_week_start=None,
            weeks_ago=None,
            change_percent=None,
            percent_per_week=None,
        )
    current_avg = weekly_avg[current_key]

    # Walk backward from the week before current_week_start to find the
    # most recent PRIOR week that actually has data — not necessarily the
    # immediately preceding calendar week, since this exercise may not be
    # trained every single week.
    prior_week_start = None
    weeks_ago = None
    cursor = current_week_start - timedelta(days=7)
    for i in range(1, 13):  # cap the walk-back at the same 12-week lookback exercise_history uses
        key = cursor.isoformat()
        if key in weekly_avg:
            prior_week_start = cursor
            weeks_ago = i
            break
        cursor -= timedelta(days=7)

    if prior_week_start is None:
        return OverloadMetricVerdict(
            verdict="INSUFFICIENT_DATA",
            current_week_avg=current_avg,
            prior_week_avg=None,
            prior_week_start=None,
            weeks_ago=None,
            change_percent=None,
            percent_per_week=None,
        )

    prior_avg = weekly_avg[prior_week_start.isoformat()]
    change_percent = (current_avg - prior_avg) / prior_avg * 100.0 if prior_avg > 0 else None
    # weeks_ago normalizes across a gap of more than 1 week (this exercise
    # skipped a week or more) — a 6% change found 3 weeks back is a ~2%/week
    # rate, not a 6%/week spike.
    percent_per_week = change_percent / weeks_ago if change_percent is not None else None

    if percent_per_week is None:
        verdict = "STALLED"
    elif abs(percent_per_week) < _OVERLOAD_RATE_STALLED_PCT_PER_WEEK:
        verdict = "STALLED"
    elif percent_per_week < 0:
        verdict = "DOWN"
    elif percent_per_week > _OVERLOAD_RATE_TOO_FAST_PCT_PER_WEEK:
        verdict = "UP_TOO_FAST"
    else:
        verdict = "UP_HEALTHY"

    return OverloadMetricVerdict(
        verdict=verdict,
        current_week_avg=current_avg,
        prior_week_avg=prior_avg,
        prior_week_start=prior_week_start.isoformat(),
        weeks_ago=weeks_ago,
        change_percent=change_percent,
        percent_per_week=percent_per_week,
    )


def progressive_overload_verdict(
    conn: sqlite3.Connection, exercise_id: str, *, weeks: int = 12, today: str | None = None
) -> ProgressiveOverloadVerdict:
    """Independent e1RM and tonnage verdicts, comparing this week's average
    against the average of the last non-empty prior week — see this
    function's module-level comment block for why week-to-week averaging
    was chosen over session-to-session or first-vs-last-over-12-weeks.
    e1RM only counts sessions with a qualifying best_e1rm (see
    best_estimated_1rm); tonnage counts every session point (tonnage is 0,
    not None, when nothing qualified — a real value, not a gap)."""
    today_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(UTC).date()
    current_week_start = _monday_of(today_date)

    points = exercise_history(conn, exercise_id, weeks=weeks, today=today)

    e1rm_points = [(p.session_date, p.best_e1rm) for p in points if p.best_e1rm is not None]
    tonnage_points = [(p.session_date, p.tonnage) for p in points]

    return ProgressiveOverloadVerdict(
        exercise_id=exercise_id,
        point_count=len(points),
        e1rm=_overload_metric_verdict(e1rm_points, current_week_start=current_week_start),
        tonnage=_overload_metric_verdict(tonnage_points, current_week_start=current_week_start),
    )
