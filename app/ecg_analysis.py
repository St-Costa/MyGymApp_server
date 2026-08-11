"""Server-side ECG waveform analysis, ported from the phone's
`EcgAnalyzer.kt` (now unreferenced/removed there) per
docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md.

Every formula/threshold here is transcribed from that handoff doc — itself
a from-source description of the last-shipped Kotlin, not a redesign. Where
the handoff doc calls out a deliberate quirk (the trailing-max fix in §1,
the asymmetric "irregular beat" definition in §4), the comment here points
back to it rather than re-explaining the reasoning from scratch.

Structure mirrors the handoff doc's own section numbers (§1-§5). Pure
functions throughout — sample list in, dataclass/plain values out — so this
is testable without a database or FastAPI, exactly like app.parsing.

No numpy/scipy: the project has no numeric dependency today, and gym-session
ECG recordings (tens of thousands of samples, not millions) don't need one
for these O(n) formulas. Introducing one for this alone would be exactly the
kind of speculative dependency CLAUDE.md's conventions ask to avoid.
"""

import math
from collections import deque
from dataclasses import dataclass

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
# Orchestration
# ---------------------------------------------------------------------------


def analyze_ecg_recording(
    session_id: str, samples: list[int], sample_rate: int, *, file_size_bytes: int
) -> EcgAnalysisResult:
    """Runs §1-§5 against one recording's decoded sample stream.

    Returns an EcgAnalysisResult with all twelve metric fields set to `None`
    (not 0) when the recording doesn't clear the handoff doc's minimum-input
    thresholds — mirroring the phone's "no result" convention rather than
    reporting misleadingly clean zeros for a recording too short/sparse to
    say anything about. `peak_count` is always populated (0 if nothing was
    detected) since it's diagnostic, not one of the twelve ported metrics.
    """
    peaks = detect_r_peaks(samples, sample_rate) if sample_rate > 0 else []

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
        )

    hrv = compute_hrv_metrics(rr)
    poincare = compute_poincare_metrics(rr, hrv.sdnn)
    arrhythmia = compute_arrhythmia_markers(rr)
    afib_episodes = compute_afib_suspicion_episodes(rr)

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
