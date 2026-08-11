"""Unit tests for app.ecg_analysis — the ported §1-§5 analysis pipeline from
docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md. Expected values below are
computed by hand against the handoff doc's formulas (see PR description /
commit message for the by-hand derivations), not just "whatever the code
currently outputs" — a wrong port should fail these, not just codify itself.
"""

import random
import struct

import pytest

from app.ecg_analysis import (
    analyze_ecg_recording,
    compute_afib_suspicion_episodes,
    compute_arrhythmia_markers,
    compute_hrv_metrics,
    compute_poincare_metrics,
    detect_r_peaks,
    rr_intervals_ms,
)
from tests.conftest import synthetic_ecg_samples

# ---------------------------------------------------------------------------
# §2 HRV metrics
# ---------------------------------------------------------------------------


def test_constant_rr_gives_zero_variability() -> None:
    """Perfectly regular 75bpm rhythm (RR=800ms constant): RMSSD/SDNN/pnn50
    must all be exactly 0 — there's no beat-to-beat variation to measure."""
    rr = [800.0] * 20
    metrics = compute_hrv_metrics(rr)
    assert metrics.avg_hr == pytest.approx(75.0)
    assert metrics.session_rmssd == 0.0
    assert metrics.sdnn == 0.0
    assert metrics.pnn50 == 0.0


def test_alternating_rr_below_pnn50_threshold() -> None:
    """RR alternating 800/850ms: consecutive diff is exactly 50ms, which is
    *not* > 50ms (handoff doc's nn50 predicate), so pnn50 must be 0 despite
    nonzero RMSSD/SDNN — this is the boundary case that most easily breaks
    with an off-by-one on the comparison operator."""
    rr = [800.0, 850.0] * 10
    metrics = compute_hrv_metrics(rr)
    assert metrics.avg_hr == pytest.approx(60000.0 / 825.0)
    assert metrics.session_rmssd == pytest.approx(50.0)
    assert metrics.sdnn == pytest.approx(25.0)
    assert metrics.pnn50 == 0.0


def test_alternating_rr_above_pnn50_threshold() -> None:
    """RR alternating 800/900ms: every consecutive diff is 100ms > 50ms, so
    every pair counts — pnn50 must be exactly 100%."""
    rr = [800.0, 900.0] * 10
    metrics = compute_hrv_metrics(rr)
    assert metrics.session_rmssd == pytest.approx(100.0)
    assert metrics.sdnn == pytest.approx(50.0)
    assert metrics.pnn50 == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# §2 RR interval extraction/filtering
# ---------------------------------------------------------------------------


def test_rr_intervals_computed_from_peak_positions() -> None:
    sample_rate = 130
    peaks = [0, 104, 208, 312]  # 104 samples apart -> 800ms at 130Hz
    rr = rr_intervals_ms(peaks, sample_rate)
    assert rr == pytest.approx([800.0, 800.0, 800.0])


def test_rr_intervals_outside_plausible_range_are_dropped() -> None:
    """300-2500ms is the handoff doc's physiological plausibility filter —
    an interval outside it (here: a 100ms gap, 600bpm, clearly a detection
    artifact) must be dropped, not passed through to the HRV math."""
    sample_rate = 130
    peaks = [0, 13, 117, 221]  # 13 samples = ~100ms (too fast), then two normal 800ms gaps
    rr = rr_intervals_ms(peaks, sample_rate)
    assert rr == pytest.approx([800.0, 800.0])


# ---------------------------------------------------------------------------
# §3 Poincaré metrics
# ---------------------------------------------------------------------------


def test_poincare_zero_for_constant_rr() -> None:
    rr = [800.0] * 20
    metrics = compute_hrv_metrics(rr)
    poincare = compute_poincare_metrics(rr, metrics.sdnn)
    assert poincare.sd1 == 0.0
    assert poincare.sd2 == 0.0
    assert poincare.ratio == 0.0


def test_poincare_sd2_clamped_to_zero_when_negative_under_sqrt() -> None:
    """handoff doc §3: sd2 = sqrt(2*sdnn^2 - 0.5*msd), clamped to 0.0 if the
    value under the sqrt goes negative — construct RR values where msd
    dominates sdnn^2 enough to trigger the clamp, and confirm no exception
    and no NaN."""
    # Two-point RR series alternating far apart: msd = mean_diff^2 is large
    # relative to sdnn^2 for a short/sharp alternation.
    rr = [500.0, 2000.0] * 3
    metrics = compute_hrv_metrics(rr)
    poincare = compute_poincare_metrics(rr, metrics.sdnn)
    assert poincare.sd2 >= 0.0


# ---------------------------------------------------------------------------
# §4 Arrhythmia markers
# ---------------------------------------------------------------------------


def test_pause_over_2000ms_is_counted() -> None:
    rr = [800.0] * 5 + [2100.0] + [800.0] * 5
    markers = compute_arrhythmia_markers(rr)
    assert markers.pause_count == 1
    assert markers.pac_count == 0


def test_pac_needs_short_rr_followed_by_compensatory_long_rr() -> None:
    """A short RR (<85% of local median) alone doesn't count — it needs a
    compensatory long RR (>110% of local median) immediately after."""
    rr = [800.0] * 6 + [600.0, 950.0] + [800.0] * 6
    markers = compute_arrhythmia_markers(rr)
    assert markers.pac_count == 1


def test_short_rr_without_compensatory_pause_is_not_counted() -> None:
    """Same short RR as above, but *not* followed by a compensatory long
    one — must not be counted as a PAC (handoff doc: "short RR without a
    compensatory pause — NOT counted as anything")."""
    rr = [800.0] * 6 + [600.0, 810.0] + [800.0] * 6
    markers = compute_arrhythmia_markers(rr)
    assert markers.pac_count == 0


def test_irregular_beat_requires_two_consecutive_deviations() -> None:
    """A single RR interval >20% above the local median, on its own,
    doesn't count — it needs a *second* consecutive deviation to confirm,
    at which point both beats are counted (+= 2)."""
    # One isolated long deviation, surrounded by regular beats: should not count.
    rr_single = [800.0] * 6 + [1000.0] + [800.0] * 6
    assert compute_arrhythmia_markers(rr_single).irregular_beats == 0

    # Two consecutive long deviations: confirms both, +2.
    rr_double = [800.0] * 6 + [1000.0, 1010.0] + [800.0] * 6
    assert compute_arrhythmia_markers(rr_double).irregular_beats == 2


def test_irregular_beat_definition_is_asymmetric_by_design() -> None:
    """handoff doc §4: only deviations *longer* than the local median count
    as "irregular" — a deliberate asymmetry, not a bug. Two consecutive
    *short* (but not short enough to be a PAC candidate — between 85% and
    100% of median) deviations must not be counted as irregular beats."""
    # 700ms is within [0.85*800=680, 800) of an 800ms median: not a PAC
    # candidate (not < 0.85*median) and not > median, so neither branch fires.
    rr = [800.0] * 6 + [700.0, 700.0] + [800.0] * 6
    markers = compute_arrhythmia_markers(rr)
    assert markers.irregular_beats == 0


# ---------------------------------------------------------------------------
# §5 AFib suspicion screening
# ---------------------------------------------------------------------------


def test_perfectly_regular_rhythm_has_no_afib_suspicion() -> None:
    rr = [800.0] * 60
    assert compute_afib_suspicion_episodes(rr) == 0


def test_highly_irregular_random_rhythm_flags_suspicion() -> None:
    """High coefficient of variation + low autocorrelation (AFib-like
    randomness) over a long-enough window must raise at least one episode."""
    rng = random.Random(42)
    rr = [800.0 + rng.uniform(-200, 200) for _ in range(60)]
    assert compute_afib_suspicion_episodes(rr) >= 1


def test_afib_episodes_count_rising_edges_only() -> None:
    """A multi-window irregular run counts once, at its start — not once per
    overlapping window it spans. Construct a long irregular stretch and
    confirm it doesn't inflate the count beyond a small number of edges."""
    rng = random.Random(7)
    regular = [800.0] * 40
    irregular = [800.0 + rng.uniform(-200, 200) for _ in range(90)]
    rr = regular + irregular + regular
    episodes = compute_afib_suspicion_episodes(rr)
    # One contiguous irregular run in the middle should produce a small
    # number of rising edges, not one per overlapping 30-window step.
    assert 1 <= episodes <= 3


# ---------------------------------------------------------------------------
# §1 R-peak detection
# ---------------------------------------------------------------------------


def _synthetic_ecg_with_true_peaks(
    sample_rate: int, duration_s: int, bpm: float, *, seed: int
) -> tuple[list[int], list[int]]:
    """Same construction as tests.conftest.synthetic_ecg_samples, but also
    returns the true beat positions — needed only by the peak-position
    accuracy test below, so kept local rather than folded into the shared
    helper (which every other caller in this file/test_ecg_endpoint.py just
    needs the sample list from)."""
    rng = random.Random(seed)
    n = sample_rate * duration_s
    samples = [rng.randint(-5, 5) for _ in range(n)]
    beat_interval = round(60.0 / bpm * sample_rate)
    peaks = list(range(beat_interval, n - beat_interval, beat_interval))
    for p in peaks:
        for offset in range(-3, 4):
            if 0 <= p + offset < n:
                samples[p + offset] += 800 - abs(offset) * 100
    return samples, peaks


def test_detect_r_peaks_finds_synthetic_beats_after_warmup() -> None:
    """The trailing-max threshold needs a short warm-up before it stabilizes
    (see detect_r_peaks' docstring) — some spurious detections in the very
    first ~1s are an expected characteristic of adaptive-threshold detection
    with a short history, not a correctness bug. After warm-up, detection
    must match the true beat count closely."""
    sample_rate = 130
    samples, true_peaks = _synthetic_ecg_with_true_peaks(sample_rate, duration_s=20, bpm=75, seed=1)
    detected = detect_r_peaks(samples, sample_rate)

    after_warmup = [p for p in detected if p > true_peaks[0] - 20]
    assert len(after_warmup) == len(true_peaks)
    # Allow the fixed processing-chain phase delay (filters aren't zero-lag)
    # but each detected peak must land close to its true position.
    for true_p, detected_p in zip(true_peaks, after_warmup, strict=True):
        assert abs(detected_p - true_p) < sample_rate * 0.15  # within 150ms


def test_detect_r_peaks_empty_input_returns_no_peaks() -> None:
    assert detect_r_peaks([], 130) == []


def test_detect_r_peaks_respects_refractory_period() -> None:
    """Two huge spikes closer together than the 250ms refractory period must
    only yield one detected peak, not two."""
    sample_rate = 130
    n = sample_rate * 3
    samples = [0] * n
    # Two spikes only 50ms (6-7 samples) apart — well inside the refractory window.
    for base in (n // 2, n // 2 + 7):
        for offset in range(-2, 3):
            samples[base + offset] += 1000
    peaks = detect_r_peaks(samples, sample_rate)
    # Both spikes are within one refractory window of each other, so at most
    # one peak should be recorded from this pair (plus possibly none, if
    # neither clears threshold in isolation — but not two).
    close_peaks = [p for p in peaks if abs(p - n // 2) < sample_rate]
    assert len(close_peaks) <= 1


# ---------------------------------------------------------------------------
# Gap-filling extension (not part of the handoff doc's §1 — see
# _fill_missed_peaks' module-level comment in app.ecg_analysis for why this
# was added: verified against real gym-session recordings where a sustained
# wide artifact silently masked several real beats, see commit message).
# ---------------------------------------------------------------------------


def _synthetic_ecg_with_sustained_artifact(
    sample_rate: int, duration_s: int, bpm: float, *, artifact_after_beat: int, seed: int
) -> tuple[list[int], list[int]]:
    """Same construction as _synthetic_ecg_with_true_peaks, plus a sustained
    600ms amplitude plateau (wider than a ~150ms QRS, and large enough to
    dominate the 5s trailing local_max — matched to the amplitude seen in
    the real recording that surfaced this failure mode, see commit message)
    injected shortly after one beat: real-amplitude beats in the following
    seconds then fall under the primary pass's 0.5x threshold."""
    samples, true_peaks = _synthetic_ecg_with_true_peaks(sample_rate, duration_s, bpm, seed=seed)
    artifact_start = true_peaks[artifact_after_beat] + round(0.1 * sample_rate)
    artifact_len = round(0.6 * sample_rate)
    n = len(samples)
    for i in range(artifact_start, min(artifact_start + artifact_len, n)):
        samples[i] += 1500
    return samples, true_peaks


def test_sustained_artifact_masks_beats_without_gap_filling() -> None:
    """Establishes the failure mode _fill_missed_peaks exists to fix: the
    raw §1 pass alone (_detect_r_peaks_raw) must miss at least one real beat
    inside the artifact's aftermath, confirming this test scenario actually
    reproduces the bug rather than accidentally being too easy."""
    from app.ecg_analysis import _detect_r_peaks_raw

    sample_rate = 130
    samples, true_peaks = _synthetic_ecg_with_sustained_artifact(
        sample_rate, duration_s=30, bpm=75, artifact_after_beat=5, seed=1
    )
    raw_peaks, _ = _detect_r_peaks_raw(samples, sample_rate)

    window_true = [p for p in true_peaks if true_peaks[5] < p < true_peaks[9]]
    window_detected = [p for p in raw_peaks if true_peaks[5] < p < true_peaks[9]]
    assert len(window_detected) < len(window_true)


def test_gap_filling_recovers_beats_missed_by_a_sustained_artifact() -> None:
    """The actual fix: detect_r_peaks (raw pass + gap-filling) must recover
    the beats the raw pass alone misses in test_sustained_artifact_masks_
    beats_without_gap_filling, bringing the count back close to true."""
    sample_rate = 130
    samples, true_peaks = _synthetic_ecg_with_sustained_artifact(
        sample_rate, duration_s=30, bpm=75, artifact_after_beat=5, seed=1
    )
    filled_peaks = detect_r_peaks(samples, sample_rate)

    window_true = [p for p in true_peaks if true_peaks[5] < p < true_peaks[9]]
    window_filled = [p for p in filled_peaks if true_peaks[5] < p < true_peaks[9]]
    assert len(window_filled) >= len(window_true) - 1  # allow at most one still missed


def test_gap_filling_does_not_alter_a_clean_regular_signal() -> None:
    """On a signal with no suspicious gaps (regular, artifact-free), the
    gap-filling pass must be a no-op — it should never manufacture peaks in
    a genuinely quiet/regular stretch."""
    sample_rate = 130
    samples = synthetic_ecg_samples(sample_rate, duration_s=20, bpm=75, seed=1)

    from app.ecg_analysis import _detect_r_peaks_raw

    raw_peaks, _ = _detect_r_peaks_raw(samples, sample_rate)
    filled_peaks = detect_r_peaks(samples, sample_rate)
    assert filled_peaks == raw_peaks


def test_gap_filling_still_respects_the_refractory_period() -> None:
    """A recovered peak must never land closer than the refractory period
    to its neighbors — the relaxed threshold only widens what counts as a
    peak's *amplitude*, never loosens the minimum spacing between beats."""
    sample_rate = 130
    samples, true_peaks = _synthetic_ecg_with_sustained_artifact(
        sample_rate, duration_s=30, bpm=75, artifact_after_beat=5, seed=1
    )
    filled_peaks = detect_r_peaks(samples, sample_rate)
    refractory = round(0.25 * sample_rate)
    for a, b in zip(filled_peaks, filled_peaks[1:], strict=False):
        assert (b - a) > refractory


# ---------------------------------------------------------------------------
# Orchestration / minimum-input thresholds
# ---------------------------------------------------------------------------


def test_below_minimum_duration_returns_no_result() -> None:
    """Fewer than sample_rate*5 samples: analysis must return None for every
    metric (not zeros) — "no result", per the handoff doc's minimum-input
    thresholds."""
    sample_rate = 130
    samples = [0] * (sample_rate * 2)  # only 2 seconds
    result = analyze_ecg_recording(
        "s1", samples, sample_rate, file_size_bytes=20 + len(samples) * 2
    )
    assert result.avg_hr is None
    assert result.session_rmssd is None
    assert result.pac_count is None
    assert result.afib_suspicion_episodes is None
    assert result.peak_count == 0  # still populated — diagnostic, not gated


def test_below_minimum_file_size_returns_no_result() -> None:
    sample_rate = 130
    samples = [1000] * (sample_rate * 10)  # plenty of samples...
    result = analyze_ecg_recording("s1", samples, sample_rate, file_size_bytes=10)  # ...tiny file
    assert result.avg_hr is None


def test_too_few_detected_peaks_returns_no_result() -> None:
    """A flat/silent recording long enough in duration but with no real QRS
    shape won't clear the >=2-peaks gate."""
    sample_rate = 130
    samples = [0] * (sample_rate * 10)
    result = analyze_ecg_recording(
        "s1", samples, sample_rate, file_size_bytes=20 + len(samples) * 2
    )
    assert result.avg_hr is None
    assert result.peak_count < 2


def test_full_pipeline_produces_a_result_for_a_realistic_recording() -> None:
    sample_rate = 130
    samples = synthetic_ecg_samples(sample_rate, duration_s=30, bpm=70, seed=3)
    result = analyze_ecg_recording(
        "s1", samples, sample_rate, file_size_bytes=20 + len(samples) * 2
    )
    assert result.session_id == "s1"
    assert result.avg_hr is not None
    assert 40 < result.avg_hr < 120  # sane heart rate range
    assert result.peak_count > 0
    assert result.pac_count is not None
    assert result.afib_suspicion_episodes is not None


def test_zero_sample_rate_returns_no_result_without_crashing() -> None:
    """A malformed/parked recording (see ECG_SPEC.md's bad-magic path, which
    stores a sample_rate=0 placeholder row) must not raise a ZeroDivisionError."""
    result = analyze_ecg_recording("s1", [1, 2, 3], 0, file_size_bytes=100)
    assert result.avg_hr is None
    assert result.peak_count == 0


def test_analyze_ecg_file_end_to_end_from_raw_bytes() -> None:
    """analyze_ecg_file's job is decoding the binary header+samples and
    delegating — covered end-to-end here rather than mocking parse_ecg_file/
    read_ecg_samples, since the whole point is that they compose correctly."""
    from app.ecg_analysis import analyze_ecg_file

    sample_rate = 130
    samples = synthetic_ecg_samples(sample_rate, duration_s=15, bpm=80, seed=5)
    header = b"MYGMECG1" + struct.pack(">i", sample_rate) + struct.pack(">q", 0)
    body = b"".join(struct.pack(">h", max(-32768, min(32767, s))) for s in samples)
    result = analyze_ecg_file("s2", header + body)
    assert result.session_id == "s2"
    assert result.avg_hr is not None
