"""Server-side ECG waveform analysis, ported from the phone's
`EcgAnalyzer.kt` (now unreferenced/removed there) per
docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md, plus a tier-1 "advanced
metrics" layer per docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md (signal
quality indices, DFA-alpha1, frequency-domain/nonlinear HRV, heart rate
recovery).

Every formula/threshold in the §1-§5 section below is transcribed from the
handoff doc — itself a from-source description of the last-shipped Kotlin,
not a redesign. Where the handoff doc calls out a deliberate quirk (the
trailing-max fix in §1, the asymmetric "irregular beat" definition in §4),
the comment here points back to it rather than re-explaining the reasoning
from scratch. The advanced-metrics section below it is new work, not a
port — see ECG_ADVANCED_ANALYSIS.md for why each metric was chosen and its
literature backing.

Structure mirrors the handoff doc's own section numbers (§1-§5), then the
advanced-metrics functions. Pure functions throughout — sample list in,
dataclass/plain values out — so this is testable without a database or
FastAPI, exactly like app.parsing.

Uses numpy/scipy/neurokit2 (via the advanced-metrics section only — the
ported §1-§5 core above stays pure-Python, unchanged): DFA/frequency-domain
HRV/entropy are well-defined statistical estimators with mature, maintained
implementations (neurokit2) worth reusing rather than hand-rolling Lomb-
Scargle PSD estimation or sample entropy from scratch — a different
tradeoff than the core §1-§5 port, which needed to reproduce a *specific*
existing algorithm exactly, not implement a generic textbook statistic.
"""

import math
import warnings
from collections import deque
from dataclasses import dataclass

import neurokit2 as nk
import numpy as np
from ecgdetectors import Detectors
from scipy.stats import kurtosis as _scipy_kurtosis

from app.parsing import parse_ecg_file, read_ecg_samples
from app.storage import EcgAnalysisResult

# Minimum input requirements before analysis proceeds at all (handoff doc
# "Minimum input requirements"). Below these, callers get "no result"
# (all twelve metric fields None) rather than zeros — zero HRV/arrhythmia
# counts would misleadingly look like "analyzed, and found nothing", when
# the truth is "too little signal to say anything at all".
_MIN_FILE_BYTES = 32
_MIN_SECONDS = 5
_MIN_PEAKS = 2
_MIN_RR_INTERVALS = 5

# RR interval physiological plausibility filter (handoff doc §2): 300ms
# (200 bpm) to 2500ms (24 bpm) — outside this range an interval is treated
# as a detection artifact, not a real heartbeat, and dropped before any HRV
# math sees it.
_RR_MIN_MS = 300.0
_RR_MAX_MS = 2500.0


# ---------------------------------------------------------------------------
# §1 R-peak detection — simplified Pan-Tompkins
# ---------------------------------------------------------------------------


def _moving_average(values: list[float], window: int) -> list[float]:
    """Centered-enough trailing moving average matching the handoff doc's
    `moving_average` calls: a simple causal running mean, window samples
    wide (clamped to >=1). Used for both the bandpass cascade (§1 step 1)
    and the QRS-integration step (§1 step 4) — same primitive, different
    window sizes."""
    window = max(1, window)
    n = len(values)
    out = [0.0] * n
    running_sum = 0.0
    for i in range(n):
        running_sum += values[i]
        if i >= window:
            running_sum -= values[i - window]
        count = min(i + 1, window)
        out[i] = running_sum / count
    return out


def _derivative(hp: list[float]) -> list[float]:
    """§1 step 2: 5-point derivative. Zero at the boundaries (i < 2 or
    i >= n-2) per the handoff doc — those samples can't see a full 5-point
    window, and the phone's implementation left them at 0 rather than using
    a shorter/asymmetric stencil there."""
    n = len(hp)
    out = [0.0] * n
    for i in range(2, n - 2):
        out[i] = (-hp[i - 2] - 2 * hp[i - 1] + 2 * hp[i + 1] + hp[i + 2]) / 8.0
    return out


def _trailing_max(values: list[float], window: int) -> list[float]:
    """O(n) trailing max over the last `window` samples via a monotonic
    deque — the handoff doc's single most load-bearing detail (§1 "Why a
    trailing max, not a global max"). A global max lets one motion-artifact
    spike (strap shift, a heavy rep) pin the detection threshold far above
    every real QRS complex for the rest of a multi-hour recording, silently
    zeroing out beat detection. A 5-second trailing max instead means one
    artifact only poisons detection for the few seconds around itself.
    **Do not replace this with a global max.**
    """
    n = len(values)
    out = [0.0] * n
    dq: deque[int] = deque()  # indices, values decreasing front-to-back
    for i in range(n):
        while dq and values[dq[-1]] <= values[i]:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - window:
            dq.popleft()
        out[i] = values[dq[0]]
    return out


@dataclass
class _PanTompkinsIntermediates:
    """Bundles the processing-chain arrays §1's primary pass produces, so
    _fill_missed_peaks (a post-processing extension, not part of the
    handoff doc's §1 itself) can re-run the same threshold logic inside a
    gap without recomputing the filter cascade."""

    integrated: list[float]
    local_max: list[float]
    refractory: int


def _detect_r_peaks_raw(
    samples: list[int], sample_rate: int
) -> tuple[list[int], _PanTompkinsIntermediates]:
    """§1, verbatim: the handoff doc's algorithm exactly as specified —
    bandpass cascade, 5-point derivative, moving-window integration,
    adaptive trailing-max threshold. Returns both the detected peaks and
    the intermediate arrays, so detect_r_peaks' gap-filling pass below can
    reuse them instead of reprocessing the raw signal a second time."""
    if not samples:
        return [], _PanTompkinsIntermediates(integrated=[], local_max=[], refractory=0)

    raw = [float(s) for s in samples]

    lp = _moving_average(raw, max(2, sample_rate // 15))
    lp_long = _moving_average(lp, max(1, sample_rate // 5))
    hp = [a - b for a, b in zip(lp, lp_long, strict=True)]

    deriv = _derivative(hp)
    squared = [d * d for d in deriv]
    integrated = _moving_average(squared, round(0.150 * sample_rate))

    refractory = round(0.25 * sample_rate)
    local_max = _trailing_max(integrated, sample_rate * 5)

    peaks: list[int] = []
    last_peak = -refractory - 1  # so the very first candidate isn't refractory-blocked
    n = len(integrated)
    for i in range(1, n - 1):
        threshold = local_max[i] * 0.5
        if (
            integrated[i] > threshold
            and integrated[i] > integrated[i - 1]
            and integrated[i] >= integrated[i + 1]
            and (i - last_peak) > refractory
        ):
            peaks.append(i)
            last_peak = i

    return peaks, _PanTompkinsIntermediates(
        integrated=integrated, local_max=local_max, refractory=refractory
    )


# Post-processing extension, *not* part of the handoff doc's §1 — added
# after verifying the straight port against real gym-session recordings
# (see commit message): a sustained wide artifact (electrode contact during
# a heavy rep, lasting well past a QRS complex's ~150ms) can pin local_max
# high enough for several seconds that real, normal-amplitude beats in that
# window fall under the 0.5x threshold and are silently missed — the same
# failure mode the handoff doc's trailing-max fix addressed for a *global*
# max, just recurring locally when an artifact is wide/sustained rather
# than a brief spike. A missed beat then shows up as one implausibly long
# RR interval, which single-handedly wrecks RMSSD/SDNN/AFib-screening for
# the whole recording. This pass looks inside each suspiciously long gap
# for weaker local maxima the primary pass's 0.5x threshold rejected, using
# a relaxed 0.25x floor scoped to *that gap only* — never applied session-
# wide, so it can't manufacture peaks in a genuinely quiet stretch.
_GAP_FILL_RR_RATIO = 1.7  # a gap must be >1.7x the local median RR to be "suspicious"
_GAP_FILL_THRESHOLD_RATIO = 0.25  # relaxed threshold, scoped to the gap window only


def _fill_missed_peaks(
    peaks: list[int], intermediates: _PanTompkinsIntermediates, sample_rate: int
) -> list[int]:
    integrated = intermediates.integrated
    local_max = intermediates.local_max
    refractory = intermediates.refractory
    if len(peaks) < 3 or not integrated:
        return peaks

    filled = [peaks[0]]
    for prev, nxt in zip(peaks, peaks[1:], strict=False):
        gap = nxt - prev
        # Local rhythm estimate: median of the (up to) 5 gaps before this
        # one, so the "suspicious" comparison adapts to the recording's own
        # current heart rate rather than a fixed bpm assumption.
        recent_pairs = zip(filled[-6:], filled[-5:], strict=False)
        recent_gaps = [b - a for a, b in recent_pairs] if len(filled) >= 2 else []
        local_rhythm = _median(recent_gaps) if recent_gaps else gap

        if local_rhythm > 0 and gap > local_rhythm * _GAP_FILL_RR_RATIO:
            # Re-scan strictly inside (prev, nxt) with the relaxed threshold,
            # respecting the same refractory period as the primary pass so
            # this can't produce physiologically impossible back-to-back beats.
            candidates: list[int] = []
            last = prev
            for i in range(prev + 1, nxt):
                relaxed_threshold = local_max[i] * _GAP_FILL_THRESHOLD_RATIO
                if (
                    integrated[i] > relaxed_threshold
                    and integrated[i] > integrated[i - 1]
                    and integrated[i] >= integrated[i + 1]
                    and (i - last) > refractory
                ):
                    candidates.append(i)
                    last = i
            filled.extend(candidates)

        filled.append(nxt)

    return filled


def detect_r_peaks(samples: list[int], sample_rate: int) -> list[int]:
    """§1 (_detect_r_peaks_raw) plus the gap-filling extension above. See
    _fill_missed_peaks' module-level comment for why this exists — the
    primary detection pass is unmodified from the handoff doc, this only
    recovers beats it's known to systematically miss inside a wide artifact."""
    peaks, intermediates = _detect_r_peaks_raw(samples, sample_rate)
    return _fill_missed_peaks(peaks, intermediates, sample_rate)


# ---------------------------------------------------------------------------
# §2 RR intervals and basic HRV
# ---------------------------------------------------------------------------


@dataclass
class HrvMetrics:
    avg_hr: float
    session_rmssd: float
    sdnn: float
    pnn50: float


def rr_intervals_ms(peaks: list[int], sample_rate: int) -> list[float]:
    """§2: consecutive-peak differences converted to milliseconds, filtered
    to the physiologically plausible 300-2500ms range."""
    raw = [(b - a) / sample_rate * 1000.0 for a, b in zip(peaks, peaks[1:], strict=False)]
    return [rr for rr in raw if _RR_MIN_MS <= rr <= _RR_MAX_MS]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def compute_hrv_metrics(rr: list[float]) -> HrvMetrics:
    """§2. Caller (analyze_ecg_recording) is responsible for the
    `len(rr) >= _MIN_RR_INTERVALS` gate — this assumes at least 2 intervals
    (1 pair) so mean_rr is well-defined; it's never called below that
    threshold in practice."""
    mean_rr = _mean(rr)
    avg_hr = 60000.0 / mean_rr

    diffs_sq = [(b - a) ** 2 for a, b in zip(rr, rr[1:], strict=False)]
    session_rmssd = math.sqrt(_mean(diffs_sq)) if diffs_sq else 0.0

    sdnn = math.sqrt(_mean([(x - mean_rr) ** 2 for x in rr]))

    nn50 = sum(1 for a, b in zip(rr, rr[1:], strict=False) if abs(b - a) > 50.0)
    pnn50 = (nn50 / len(diffs_sq) * 100.0) if diffs_sq else 0.0

    return HrvMetrics(avg_hr=avg_hr, session_rmssd=session_rmssd, sdnn=sdnn, pnn50=pnn50)


# ---------------------------------------------------------------------------
# §3 Poincaré plot metrics
# ---------------------------------------------------------------------------


@dataclass
class PoincareMetrics:
    sd1: float
    sd2: float
    ratio: float


def compute_poincare_metrics(rr: list[float], sdnn: float) -> PoincareMetrics:
    """§3. `msd` here is deliberately the same quantity as session_rmssd^2 in
    compute_hrv_metrics — the handoff doc calls this out explicitly ("same
    'msd' as RMSSD^2"), so it's recomputed directly from `rr` rather than
    threaded through as a parameter, keeping this function self-contained."""
    diffs_sq = [(b - a) ** 2 for a, b in zip(rr, rr[1:], strict=False)]
    msd = _mean(diffs_sq) if diffs_sq else 0.0

    sd1 = math.sqrt(0.5) * math.sqrt(msd)
    sd2_arg = 2 * sdnn**2 - 0.5 * msd
    sd2 = math.sqrt(sd2_arg) if sd2_arg > 0 else 0.0
    ratio = sd2 / sd1 if sd1 > 0 else 0.0

    return PoincareMetrics(sd1=sd1, sd2=sd2, ratio=ratio)


# ---------------------------------------------------------------------------
# §4 Arrhythmia markers — single pass, sliding local median (window 10)
# ---------------------------------------------------------------------------


@dataclass
class ArrhythmiaMarkers:
    pac_count: int
    pause_count: int
    irregular_beats: int


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_arrhythmia_markers(rr: list[float]) -> ArrhythmiaMarkers:
    """§4. Ported as a literal single pass over `rr`, state (`uncounted_
    consecutive`) carried between iterations exactly as the handoff doc
    describes — this is not embarrassingly parallel/vectorizable without
    changing behavior, so it stays a plain Python loop rather than being
    rewritten into a batch of list comprehensions."""
    pac_count = 0
    pause_count = 0
    irregular_beats = 0
    uncounted_consecutive = False

    n = len(rr)
    for i in range(n):
        interval = rr[i]

        if interval > 2000:
            pause_count += 1
            uncounted_consecutive = False
            continue

        start = max(0, i - 5)
        end = min(n, start + 10)
        if (end - start) < 5:
            uncounted_consecutive = False
            continue
        local_median = _median(rr[start:end])

        if interval < local_median * 0.85:
            next_rr = rr[i + 1] if i + 1 < n else None
            if next_rr is not None and next_rr > local_median * 1.10:
                pac_count += 1
            # else: short RR without a compensatory pause — not counted as anything.
            uncounted_consecutive = False

        elif abs(interval - local_median) > local_median * 0.20 and interval > local_median:
            # Deliberate asymmetry (handoff doc §4): only intervals *longer*
            # than the local median count as "irregular" here — carried over
            # as-is, not a bug to "fix" to a symmetric definition.
            if uncounted_consecutive:
                irregular_beats += 2  # confirms both the previous and current beat
                uncounted_consecutive = False
            else:
                uncounted_consecutive = True  # wait for a second consecutive deviation

        else:
            uncounted_consecutive = False

    return ArrhythmiaMarkers(
        pac_count=pac_count, pause_count=pause_count, irregular_beats=irregular_beats
    )


# ---------------------------------------------------------------------------
# §5 AFib suspicion screening — sliding-window heuristic
# ---------------------------------------------------------------------------

_AFIB_WINDOW_LEN = 30
_AFIB_STEP = 15
_AFIB_CV_THRESHOLD = 0.12
_AFIB_AUTOCORR_THRESHOLD = 0.20


def _stddev(values: list[float], mean: float) -> float:
    return math.sqrt(_mean([(x - mean) ** 2 for x in values]))


def compute_afib_suspicion_episodes(rr: list[float]) -> int:
    """§5. **Screening heuristic, not a diagnostic tool** — the handoff doc
    is explicit that the phone UI always paired this with a disclaimer
    ("non diagnostico, consulta un medico se persiste/ricorrente") and any
    server-side surfacing of this value must keep that framing; this
    function only returns the count, callers are responsible for the
    disclaimer at the presentation layer.

    Counts rising edges into an irregular window only (not every irregular
    window individually) — a multi-window irregular run counts once, at its
    start, per the handoff doc's `if irregular_window and not in_episode`."""
    episodes = 0
    in_episode = False
    n = len(rr)
    i = 0
    while i + _AFIB_WINDOW_LEN <= n:
        window = rr[i : i + _AFIB_WINDOW_LEN]
        w_mean = _mean(window)
        w_sd = _stddev(window, w_mean)
        cv = (w_sd / w_mean) if w_mean > 0 else 0.0

        pairs = list(zip(window, window[1:], strict=False))
        covariance = _mean([(a - w_mean) * (b - w_mean) for a, b in pairs]) if pairs else 0.0
        autocorr = (covariance / w_sd**2) if w_sd > 0 else 0.0

        irregular_window = cv > _AFIB_CV_THRESHOLD and abs(autocorr) < _AFIB_AUTOCORR_THRESHOLD

        if irregular_window and not in_episode:
            episodes += 1
            in_episode = True
        elif not irregular_window:
            in_episode = False

        i += _AFIB_STEP

    return episodes


# ---------------------------------------------------------------------------
# Advanced metrics (docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md) — new
# work, not a port of anything the phone used to do. Each function is
# independent and returns None on its own failure/insufficient-input case
# rather than raising, so one metric's edge case can never take out the
# others in analyze_ecg_recording's orchestration below.
# ---------------------------------------------------------------------------

# bSQI tolerance: how close (in samples) our detector's peak and the second
# detector's peak must land to count as "the same beat" for agreement
# purposes. ~40ms at 130Hz — generous enough to absorb the fixed phase delay
# between two differently-designed filter chains (see detect_r_peaks'
# gap-filling comment on why our own chain isn't zero-lag either), tight
# enough not to conflate two genuinely different beats.
_BSQI_TOLERANCE_SAMPLES = 5


def compute_kurtosis_sqi(samples: list[int]) -> float | None:
    """kSQI: kurtosis of the raw amplitude distribution. Clean QRS-dominated
    ECG has a heavy-tailed, peaked amplitude distribution (high kurtosis);
    motion artifact/noise flattens it. See ECG_ADVANCED_ANALYSIS.md for why
    this replaces the vital-sqi library the literature review pointed at
    (broken on PyPI). Returns None if there's nothing to compute over, or if
    the samples are constant/near-constant (kurtosis is undefined for zero
    variance — scipy would compute it anyway via a numerically unstable path
    and emit a RuntimeWarning about catastrophic cancellation; a flat signal
    is unambiguously "no real cardiac signal here" regardless)."""
    if len(samples) < 2:
        return None
    arr = np.asarray(samples, dtype=np.float64)
    if np.std(arr) == 0:
        return None
    value = float(_scipy_kurtosis(arr))
    return value if math.isfinite(value) else None


# Search window (samples) for phase-offset estimation — generous enough to
# cover the largest systematic delay observed between our detector and
# py-ecg-detectors' Hamilton detector on real recordings (up to ~100
# samples), without being so wide it starts pulling in unrelated beats on
# a fast rhythm (at 200bpm, RR is only ~39 samples at 130Hz — a 200-sample
# window already spans several real beats either side).
_PHASE_OFFSET_SEARCH_WINDOW = 200


def _estimate_phase_offset(primary_peaks: list[int], secondary_peaks: list[int]) -> int:
    """Estimates the systematic phase delay between two independently-
    designed detectors — found necessary against real recordings (see
    commit message): py-ecg-detectors' Hamilton detector runs a noticeably
    different filter cascade than this module's Pan-Tompkins-based chain,
    producing a *fixed* offset of tens to ~100 samples between the two
    peak streams (not noise — a consistent processing delay). Comparing
    raw positions with only a tight tolerance would then flag nearly every
    beat as "disagreement" even on a clean signal, which defeats bSQI's
    purpose (measuring real artifact disagreement, not filter-chain lag).

    Method: histogram every (primary, secondary) pair within
    _PHASE_OFFSET_SEARCH_WINDOW of each other by their signed distance, and
    take the mode. This is deliberately *not* "each primary peak's single
    nearest secondary neighbor" — that simpler approach can converge on the
    wrong side entirely when the true offset exceeds roughly half the local
    RR period (verified as a real failure mode: a uniform ~130-sample RR
    spacing with a true 75-sample offset makes the *opposite*-side neighbor
    closer for most peaks, so a nearest-neighbor-only mode locks onto the
    wrong value). Voting over every nearby pair instead means the true
    systematic offset gets a vote from *every* correctly-matched beat, while
    any wrong-side/spurious offset only gets partial support — the true
    offset reliably wins the vote even in the adversarial case that broke
    nearest-neighbor-only matching. Windowed rather than all-pairs to stay
    fast on long recordings (~12,000 peaks): O(n) per peak's window lookup
    via bisect on the sorted secondary list, not O(n*m)."""
    if not primary_peaks or not secondary_peaks:
        return 0
    secondary_sorted = sorted(secondary_peaks)

    offset_votes: dict[int, int] = {}
    for peak in primary_peaks:
        lo = _bisect_left(secondary_sorted, peak - _PHASE_OFFSET_SEARCH_WINDOW)
        hi = _bisect_left(secondary_sorted, peak + _PHASE_OFFSET_SEARCH_WINDOW + 1)
        for secondary_peak in secondary_sorted[lo:hi]:
            offset = secondary_peak - peak
            offset_votes[offset] = offset_votes.get(offset, 0) + 1

    if not offset_votes:
        return 0
    return max(offset_votes, key=lambda o: offset_votes[o])


def compute_b_sqi(samples: list[int], sample_rate: int, primary_peaks: list[int]) -> float | None:
    """bSQI: fraction of `primary_peaks` (this module's own detect_r_peaks
    output) confirmed by an independent second detector (py-ecg-detectors'
    Hamilton detector — a different filter design and peak-selection logic
    from our Pan-Tompkins-based chain), after compensating for the
    systematic phase offset between the two chains (see
    _estimate_phase_offset). Two independently-built detectors disagreeing
    on a beat *after* accounting for their fixed processing delay is real
    evidence that beat is an artifact rather than a real QRS. 1.0 = full
    agreement, 0.0 = none. None if there are no primary peaks to score
    agreement against."""
    if not primary_peaks or sample_rate <= 0:
        return None
    try:
        secondary_peaks = Detectors(sample_rate).hamilton_detector(samples)
    except Exception:
        # py-ecg-detectors can raise on pathological input (e.g. too short
        # for its own internal filter windows) — a failed cross-check is a
        # "we don't know" signal, not grounds for crashing the whole analysis.
        return None
    if not secondary_peaks:
        return 0.0

    phase_offset = _estimate_phase_offset(primary_peaks, secondary_peaks)
    secondary_sorted = sorted(secondary_peaks)
    confirmed = 0
    for peak in primary_peaks:
        target = peak + phase_offset
        lo = target - _BSQI_TOLERANCE_SAMPLES
        hi = target + _BSQI_TOLERANCE_SAMPLES
        idx = _bisect_left(secondary_sorted, lo)
        if idx < len(secondary_sorted) and secondary_sorted[idx] <= hi:
            confirmed += 1
    return confirmed / len(primary_peaks)


def _bisect_left(sorted_values: list[int], target: int) -> int:
    """Standard binary search for the leftmost insertion point — avoids
    pulling in the stdlib `bisect` module for one call site, but behaves
    identically to `bisect.bisect_left`."""
    lo, hi = 0, len(sorted_values)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


@dataclass
class NeuroKitDerivedMetrics:
    dfa_alpha1: float | None
    hrv_lf: float | None
    hrv_hf: float | None
    hrv_lf_hf: float | None
    sample_entropy: float | None
    csi: float | None
    cvi: float | None


# neurokit2's HRV functions need enough RR intervals to be meaningful, not
# just the handoff doc's _MIN_RR_INTERVALS=5 (that threshold was tuned for
# the simple time-domain metrics in §2, which tolerate short series far
# better than frequency-domain/entropy estimators do). Frequency-domain HRV
# in particular needs real time coverage, not just interval count — the
# literature's ~1 minute minimum for HF, ~5 minutes for LF/HF (see
# ECG_ADVANCED_ANALYSIS.md) is approximated here via a duration check.
_MIN_RR_FOR_NEUROKIT = 30
_MIN_SECONDS_FOR_FREQUENCY_DOMAIN = 60.0

# hrv_nonlinear's SampEn estimator is O(n^2) in the number of RR intervals —
# measured at ~1.4s for 800 points but growing to 100+ seconds by ~12,000
# points (a multi-hour gym session easily produces this many beats),
# unacceptable for the synchronous in-request analysis path
# (app.ingestion.ingest_ecg). Capped to the most recent _NONLINEAR_RR_CAP
# intervals rather than run over the whole session — an explicit, documented
# approximation (recent-window nonlinear HRV, not whole-session), not a
# silent truncation. LF/HF below is *not* capped: hrv_frequency's PSD
# estimation is O(n log n) and stayed under 0.2s even on the ~12,000-point
# case, so there's no performance reason to sacrifice its whole-session
# accuracy.
_NONLINEAR_RR_CAP = 800


def compute_neurokit_derived_metrics(
    peaks: list[int], sample_rate: int, rr: list[float]
) -> NeuroKitDerivedMetrics:
    """DFA-alpha1, frequency-domain HRV (LF/HF), and nonlinear extras
    (SampEn, CSI, CVI) — all from neurokit2.hrv_nonlinear/hrv_frequency, fed
    the same peak positions detect_r_peaks already produced (no separate
    peak detection pass). Each field is independently None if neurokit2
    couldn't compute it (e.g. LF/HF needs more time coverage than a short
    recording has, even if it clears the bare RR-count minimum) — a
    computation failing here never raises, so a single unavailable metric
    can't blank out the others."""
    if len(rr) < _MIN_RR_FOR_NEUROKIT or not peaks:
        return NeuroKitDerivedMetrics(None, None, None, None, None, None, None)

    duration_s = (peaks[-1] - peaks[0]) / sample_rate

    dfa_alpha1 = None
    sample_entropy = None
    csi = None
    cvi = None
    try:
        # See _NONLINEAR_RR_CAP's comment: capped to the most recent window
        # for performance, not run over the full peak list like hrv_frequency
        # below.
        nonlinear_peaks = np.asarray(peaks[-_NONLINEAR_RR_CAP:])
        # We only ever read HRV_DFA_alpha1 (short-term exponent), never the
        # long-term alpha2 neurokit2 also attempts — on a recording too
        # short for alpha2's larger window range it warns loudly about that
        # every single call, which is just noise for a metric this function
        # doesn't use. Suppressed narrowly (this call only, not globally).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            nonlinear = nk.hrv_nonlinear(nonlinear_peaks, sampling_rate=sample_rate, show=False)
        dfa_alpha1 = _finite_or_none(nonlinear["HRV_DFA_alpha1"].iloc[0])
        sample_entropy = _finite_or_none(nonlinear["HRV_SampEn"].iloc[0])
        csi = _finite_or_none(nonlinear["HRV_CSI"].iloc[0])
        cvi = _finite_or_none(nonlinear["HRV_CVI"].iloc[0])
    except Exception:
        # neurokit2's nonlinear estimators can fail on edge-case RR
        # distributions (e.g. DFA's internal windowing needs a minimum
        # scale range) — treated as "no result" for these fields, not a
        # reason to fail the whole analysis.
        pass

    hrv_lf = hrv_hf = hrv_lf_hf = None
    if duration_s >= _MIN_SECONDS_FOR_FREQUENCY_DOMAIN:
        try:
            freq = nk.hrv_frequency(np.asarray(peaks), sampling_rate=sample_rate, show=False)
            hrv_lf = _finite_or_none(freq["HRV_LF"].iloc[0])
            hrv_hf = _finite_or_none(freq["HRV_HF"].iloc[0])
            hrv_lf_hf = _finite_or_none(freq["HRV_LFHF"].iloc[0])
        except Exception:
            pass

    return NeuroKitDerivedMetrics(
        dfa_alpha1=dfa_alpha1,
        hrv_lf=hrv_lf,
        hrv_hf=hrv_hf,
        hrv_lf_hf=hrv_lf_hf,
        sample_entropy=sample_entropy,
        csi=csi,
        cvi=cvi,
    )


def _finite_or_none(value: float) -> float | None:
    """neurokit2/pandas can hand back NaN for a metric it "computed" but
    couldn't meaningfully estimate (e.g. insufficient variance) — NaN would
    silently corrupt any downstream comparison/aggregation, so normalize it
    to the same None ("no result") convention as everything else here."""
    return float(value) if math.isfinite(value) else None


@dataclass
class HeartRateRecoveryResult:
    hrr_1min: float | None
    hrr_2min: float | None


# Window (seconds) for smoothing instantaneous HR before searching for the
# recording's "peak" — found necessary against real gym-session recordings
# (see commit message): a single noisy/anomalous RR interval (e.g. 300ms,
# 200bpm — inside the physiological-plausibility filter but not a real
# sustained rate for a gym session) can otherwise single-handedly become
# "the peak", producing an HRR number computed from one bad beat rather
# than a genuine sustained-effort period. Real HRR is always measured
# against a sustained rate, never one instant — this averaging reflects
# that, not just a noise-reduction convenience.
_HR_SMOOTHING_WINDOW_S = 12.0


def _smooth_instantaneous_hr(inst_hr: list[float], inst_hr_times_s: list[float]) -> list[float]:
    """Trailing mean of `inst_hr` over the last _HR_SMOOTHING_WINDOW_S
    seconds at each point — O(n) via a running sum over a time-based
    (not fixed-count) window, since RR intervals aren't evenly spaced in
    time. Same shape as detect_r_peaks' _trailing_max, but a mean over a
    time window instead of a max over a sample-count window."""
    n = len(inst_hr)
    smoothed = [0.0] * n
    window_start = 0
    running_sum = 0.0
    for i in range(n):
        running_sum += inst_hr[i]
        while inst_hr_times_s[i] - inst_hr_times_s[window_start] > _HR_SMOOTHING_WINDOW_S:
            running_sum -= inst_hr[window_start]
            window_start += 1
        smoothed[i] = running_sum / (i - window_start + 1)
    return smoothed


def compute_heart_rate_recovery(peaks: list[int], sample_rate: int) -> HeartRateRecoveryResult:
    """Approximates HRR1/HRR2 using the recording's peak *smoothed* HR as an
    effort-cessation proxy — see ECG_ADVANCED_ANALYSIS.md's "Heart Rate
    Recovery" section for why this is an approximation (no true
    exercise/rest segment boundary is available to this pipeline yet) and
    its known failure modes (multi-interval sessions, no clear peak).

    Instantaneous HR is computed per-RR-interval (60000/rr_ms), then
    smoothed over _HR_SMOOTHING_WINDOW_S before the peak search — see
    _smooth_instantaneous_hr's docstring for why a single unsmoothed
    instant can't be trusted as "the peak". The recovery delta itself is
    still measured against the *smoothed* series at the target offset, for
    the same single-noisy-beat reason."""
    if len(peaks) < 3:
        return HeartRateRecoveryResult(None, None)

    inst_hr = []
    inst_hr_times_s = []
    for a, b in zip(peaks, peaks[1:], strict=False):
        rr_ms = (b - a) / sample_rate * 1000.0
        if _RR_MIN_MS <= rr_ms <= _RR_MAX_MS:
            inst_hr.append(60000.0 / rr_ms)
            inst_hr_times_s.append(b / sample_rate)

    if len(inst_hr) < 3:
        return HeartRateRecoveryResult(None, None)

    smoothed_hr = _smooth_instantaneous_hr(inst_hr, inst_hr_times_s)

    peak_idx = max(range(len(smoothed_hr)), key=lambda i: smoothed_hr[i])
    peak_hr = smoothed_hr[peak_idx]
    peak_time_s = inst_hr_times_s[peak_idx]
    recording_end_s = inst_hr_times_s[-1]

    hrr_1min = _hr_recovery_at(
        smoothed_hr, inst_hr_times_s, peak_hr, peak_time_s, 60.0, recording_end_s
    )
    hrr_2min = _hr_recovery_at(
        smoothed_hr, inst_hr_times_s, peak_hr, peak_time_s, 120.0, recording_end_s
    )
    return HeartRateRecoveryResult(hrr_1min=hrr_1min, hrr_2min=hrr_2min)


def _hr_recovery_at(
    smoothed_hr: list[float],
    inst_hr_times_s: list[float],
    peak_hr: float,
    peak_time_s: float,
    offset_s: float,
    recording_end_s: float,
) -> float | None:
    """HR drop from `peak_hr` to the *smoothed* HR nearest `offset_s`
    seconds after the peak (see compute_heart_rate_recovery's docstring for
    why smoothed, not instantaneous). None if the recording doesn't extend
    far enough past the peak to measure that offset at all — a short/no-
    cooldown recording shouldn't report a fabricated recovery number."""
    target_time = peak_time_s + offset_s
    if target_time > recording_end_s:
        return None
    # Nearest sample to the target time, by absolute distance.
    closest_idx = min(
        range(len(inst_hr_times_s)), key=lambda i: abs(inst_hr_times_s[i] - target_time)
    )
    return peak_hr - smoothed_hr[closest_idx]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def analyze_ecg_recording(
    session_id: str, samples: list[int], sample_rate: int, *, file_size_bytes: int
) -> EcgAnalysisResult:
    """Runs §1-§5 plus the advanced-metrics tier (ECG_ADVANCED_ANALYSIS.md)
    against one recording's decoded sample stream.

    Every metric field is `None` (not 0) when its own computation didn't
    clear its minimum-input requirement — mirroring the phone's "no result"
    convention rather than reporting misleadingly clean zeros. `peak_count`
    is always populated (0 if nothing was detected) since it's diagnostic.
    `kurtosis_sqi` doesn't depend on peak detection at all, so it's computed
    even when the recording is otherwise too short/sparse to analyze — it's
    useful precisely for explaining *why* a recording came back empty.
    """
    kurtosis_sqi = compute_kurtosis_sqi(samples)
    peaks = detect_r_peaks(samples, sample_rate) if sample_rate > 0 else []
    b_sqi = compute_b_sqi(samples, sample_rate, peaks) if peaks else None

    below_minimum = (
        file_size_bytes < _MIN_FILE_BYTES
        or sample_rate <= 0
        or len(samples) < sample_rate * _MIN_SECONDS
        or len(peaks) < _MIN_PEAKS
    )

    rr = rr_intervals_ms(peaks, sample_rate) if not below_minimum else []
    if below_minimum or len(rr) < _MIN_RR_INTERVALS:
        return EcgAnalysisResult(
            session_id=session_id,
            avg_hr=None,
            session_rmssd=None,
            sdnn=None,
            pnn50=None,
            poincare_sd1=None,
            poincare_sd2=None,
            poincare_ratio=None,
            pac_count=None,
            pause_count=None,
            irregular_beats=None,
            afib_suspicion_episodes=None,
            peak_count=len(peaks),
            kurtosis_sqi=kurtosis_sqi,
            b_sqi=b_sqi,
        )

    hrv = compute_hrv_metrics(rr)
    poincare = compute_poincare_metrics(rr, hrv.sdnn)
    arrhythmia = compute_arrhythmia_markers(rr)
    afib_episodes = compute_afib_suspicion_episodes(rr)
    neurokit_metrics = compute_neurokit_derived_metrics(peaks, sample_rate, rr)
    hrr = compute_heart_rate_recovery(peaks, sample_rate)

    return EcgAnalysisResult(
        session_id=session_id,
        avg_hr=hrv.avg_hr,
        session_rmssd=hrv.session_rmssd,
        sdnn=hrv.sdnn,
        pnn50=hrv.pnn50,
        poincare_sd1=poincare.sd1,
        poincare_sd2=poincare.sd2,
        poincare_ratio=poincare.ratio,
        pac_count=arrhythmia.pac_count,
        pause_count=arrhythmia.pause_count,
        irregular_beats=arrhythmia.irregular_beats,
        afib_suspicion_episodes=afib_episodes,
        peak_count=len(peaks),
        kurtosis_sqi=kurtosis_sqi,
        b_sqi=b_sqi,
        dfa_alpha1=neurokit_metrics.dfa_alpha1,
        hrv_lf=neurokit_metrics.hrv_lf,
        hrv_hf=neurokit_metrics.hrv_hf,
        hrv_lf_hf=neurokit_metrics.hrv_lf_hf,
        sample_entropy=neurokit_metrics.sample_entropy,
        csi=neurokit_metrics.csi,
        cvi=neurokit_metrics.cvi,
        hrr_1min=hrr.hrr_1min,
        hrr_2min=hrr.hrr_2min,
    )


def analyze_ecg_file(session_id: str, raw_bytes: bytes) -> EcgAnalysisResult:
    """Convenience wrapper: decodes a raw (decompressed) .ecg file exactly as
    stored under raw/ecg/{session_id}.ecg and runs analyze_ecg_recording
    against it. This is what callers with only the file bytes (the analysis
    queue processor, reparse-style re-analysis) should use; analyze_ecg_recording
    itself stays the lower-level entry point for tests that want to hand-build
    a sample list without a binary header."""
    parsed = parse_ecg_file(raw_bytes)
    samples = read_ecg_samples(raw_bytes)
    return analyze_ecg_recording(
        session_id, samples, parsed.sample_rate, file_size_bytes=len(raw_bytes)
    )
