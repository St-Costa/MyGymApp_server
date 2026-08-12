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


# ---------------------------------------------------------------------------
# Advanced metrics (docs/sync-ingestion/ECG_ADVANCED_ANALYSIS.md) — tier-1:
# signal quality (kSQI/bSQI), DFA-alpha1/frequency-domain/nonlinear HRV via
# neurokit2, and the HRR approximation. New work, not a port, so expected
# values here are sanity ranges / directional checks rather than exact
# by-hand derivations the way §1-§5's HRV formulas were.
# ---------------------------------------------------------------------------


def test_kurtosis_sqi_is_higher_for_qrs_shaped_signal_than_flat_noise() -> None:
    """A clean QRS-shaped signal has a heavy-tailed, peaked amplitude
    distribution (high kurtosis); flat low-amplitude noise doesn't."""
    from app.ecg_analysis import compute_kurtosis_sqi

    qrs_shaped = synthetic_ecg_samples(130, duration_s=20, bpm=75, seed=1)
    flat_noise = [1, -1, 2, -2, 1, -1, 0, 1, -1, 2] * 200

    qrs_kurtosis = compute_kurtosis_sqi(qrs_shaped)
    noise_kurtosis = compute_kurtosis_sqi(flat_noise)
    assert qrs_kurtosis is not None
    assert noise_kurtosis is not None
    assert qrs_kurtosis > noise_kurtosis


def test_kurtosis_sqi_none_for_constant_signal() -> None:
    """Kurtosis is undefined (zero variance) for a constant signal — must
    return None, not raise or emit scipy's catastrophic-cancellation warning
    (see compute_kurtosis_sqi's docstring)."""
    from app.ecg_analysis import compute_kurtosis_sqi

    assert compute_kurtosis_sqi([100] * 500) is None


def test_kurtosis_sqi_none_for_too_few_samples() -> None:
    from app.ecg_analysis import compute_kurtosis_sqi

    assert compute_kurtosis_sqi([1]) is None


def test_b_sqi_high_agreement_on_clean_signal() -> None:
    """Two independently-designed detectors should mostly agree on a clean,
    unambiguous synthetic QRS signal."""
    from app.ecg_analysis import compute_b_sqi, detect_r_peaks

    samples = synthetic_ecg_samples(130, duration_s=20, bpm=75, seed=1)
    peaks = detect_r_peaks(samples, 130)
    b_sqi = compute_b_sqi(samples, 130, peaks)
    assert b_sqi is not None
    assert b_sqi > 0.7


def test_estimate_phase_offset_recovers_a_shifted_but_identical_peak_set() -> None:
    """Regression test: verified against real gym-session recordings (see
    commit message) that py-ecg-detectors' Hamilton detector runs a
    different filter cascade than ours, producing a systematic phase delay
    of tens to ~100 samples versus our own detect_r_peaks output — not
    noise, a fixed offset. Uncompensated, bSQI was near-zero even on clean
    recordings. Here: an artificially shifted copy of the same peak set
    must have its shift correctly recovered."""
    from app.ecg_analysis import _estimate_phase_offset

    primary = [100, 230, 360, 490, 620, 750]
    true_offset = 75
    secondary = [p + true_offset for p in primary]
    assert _estimate_phase_offset(primary, secondary) == true_offset


def test_b_sqi_high_agreement_survives_a_large_systematic_offset() -> None:
    """Without phase-offset compensation, a detector whose peaks are all
    shifted by a large-but-fixed amount would look like near-total
    disagreement despite marking the exact same beats. With compensation,
    agreement should be high."""
    from app.ecg_analysis import _BSQI_TOLERANCE_SAMPLES, compute_b_sqi

    primary_peaks = [100 + i * 130 for i in range(50)]  # regular ~1s spacing
    large_offset = 75  # far outside _BSQI_TOLERANCE_SAMPLES on its own
    assert large_offset > _BSQI_TOLERANCE_SAMPLES * 2

    # Monkeypatch-free: call the internal confirm logic directly isn't
    # exposed, so exercise it through compute_b_sqi by faking the detector
    # via a signal shaped so Hamilton's own detection lands near
    # primary_peaks + large_offset. Simpler and more direct: build a
    # synthetic signal whose true QRS-like spikes sit at primary_peaks +
    # large_offset, and hand compute_b_sqi primary_peaks as if our detector
    # found the un-shifted positions (simulating the real phase-delay
    # scenario end to end, not just the offset-estimation helper alone).
    sample_rate = 130
    n = primary_peaks[-1] + 200
    samples = [0] * n
    for p in primary_peaks:
        shifted = p + large_offset
        for offset in range(-3, 4):
            if 0 <= shifted + offset < n:
                samples[shifted + offset] += 800 - abs(offset) * 100

    b_sqi = compute_b_sqi(samples, sample_rate, primary_peaks)
    assert b_sqi is not None
    assert b_sqi > 0.7


def test_b_sqi_none_without_primary_peaks() -> None:
    from app.ecg_analysis import compute_b_sqi

    samples = synthetic_ecg_samples(130, duration_s=20, bpm=75, seed=1)
    assert compute_b_sqi(samples, 130, []) is None


def test_b_sqi_zero_when_second_detector_finds_nothing() -> None:
    """If the primary detector found peaks but the independent second
    detector finds none at all on the same samples, agreement is 0.0 (a
    real, computed disagreement), not None — the inputs were valid, the
    detectors just disagree completely."""
    from app.ecg_analysis import compute_b_sqi

    # Flat signal: our own detector wouldn't normally produce peaks on this,
    # but by handing in synthetic "primary_peaks" directly we isolate bSQI's
    # own agreement logic from detect_r_peaks' behavior.
    flat = [0] * 2600  # 20s at 130Hz, no QRS shape at all
    b_sqi = compute_b_sqi(flat, 130, primary_peaks=[100, 500, 900])
    assert b_sqi == 0.0


def test_neurokit_derived_metrics_none_below_minimum_rr_count() -> None:
    from app.ecg_analysis import compute_neurokit_derived_metrics

    result = compute_neurokit_derived_metrics(peaks=[0, 100, 200], sample_rate=130, rr=[800.0] * 3)
    assert result.dfa_alpha1 is None
    assert result.sample_entropy is None
    assert result.hrv_lf is None


def test_neurokit_derived_metrics_produces_values_for_a_realistic_recording() -> None:
    from app.ecg_analysis import compute_neurokit_derived_metrics, detect_r_peaks, rr_intervals_ms

    sample_rate = 130
    # Long enough to clear both the RR-count minimum and the frequency-
    # domain duration minimum (60s) — synthetic_ecg_samples' regular rhythm
    # gives DFA-alpha1 a well-defined (if not clinically meaningful) value.
    samples = synthetic_ecg_samples(sample_rate, duration_s=90, bpm=75, seed=2)
    peaks = detect_r_peaks(samples, sample_rate)
    rr = rr_intervals_ms(peaks, sample_rate)

    result = compute_neurokit_derived_metrics(peaks, sample_rate, rr)
    assert result.dfa_alpha1 is not None
    assert result.sample_entropy is not None
    assert result.csi is not None
    assert result.cvi is not None
    assert result.hrv_lf is not None
    assert result.hrv_hf is not None
    assert result.hrv_lf_hf is not None


def test_neurokit_derived_metrics_frequency_domain_needs_more_duration_than_rr_count() -> None:
    """LF/HF need ~60s+ of coverage (ECG_ADVANCED_ANALYSIS.md) even if the
    bare RR-count minimum is cleared — a short, fast-paced recording with
    enough *beats* but not enough *time* should get DFA/nonlinear metrics
    but not frequency-domain ones."""
    from app.ecg_analysis import _MIN_RR_FOR_NEUROKIT, compute_neurokit_derived_metrics

    sample_rate = 130
    # Very fast synthetic rhythm (200bpm) packs enough beats into under 60s
    # to clear _MIN_RR_FOR_NEUROKIT while staying short on wall-clock time.
    samples = synthetic_ecg_samples(sample_rate, duration_s=20, bpm=200, seed=3)
    from app.ecg_analysis import detect_r_peaks, rr_intervals_ms

    peaks = detect_r_peaks(samples, sample_rate)
    rr = rr_intervals_ms(peaks, sample_rate)
    assert len(rr) >= _MIN_RR_FOR_NEUROKIT  # sanity: the scenario is set up as intended

    result = compute_neurokit_derived_metrics(peaks, sample_rate, rr)
    assert result.hrv_lf is None
    assert result.hrv_hf is None
    assert result.hrv_lf_hf is None


def test_heart_rate_recovery_drops_after_synthetic_peak() -> None:
    """Constructs an RR series with a clear HR ramp-up to ~150bpm then a
    3-minute linear decay to ~90bpm — HRR1/HRR2 should both be positive
    (HR dropped) and HRR2 > HRR1 (more recovery with more time)."""
    from app.ecg_analysis import compute_heart_rate_recovery

    sample_rate = 130
    peaks = [0]
    t_ms = 0.0
    n_up = 80
    for i in range(n_up):
        hr = 90 + (150 - 90) * (i / n_up)
        t_ms += 60000.0 / hr
        peaks.append(round(t_ms / 1000 * sample_rate))
    n_down = 300
    for i in range(n_down):
        hr = 150 - (150 - 90) * (i / n_down)
        t_ms += 60000.0 / hr
        peaks.append(round(t_ms / 1000 * sample_rate))

    result = compute_heart_rate_recovery(peaks, sample_rate)
    assert result.hrr_1min is not None
    assert result.hrr_2min is not None
    assert result.hrr_1min > 0
    assert result.hrr_2min > result.hrr_1min


def test_heart_rate_recovery_ignores_a_single_noisy_beat_as_the_peak() -> None:
    """Regression test: verified against real gym-session recordings (see
    commit message) that a single anomalous-but-plausible RR interval
    (e.g. 300ms, 200bpm — inside _RR_MIN_MS/_RR_MAX_MS's physiological
    filter but not a real sustained rate) could single-handedly become "the
    peak" before HR smoothing was added, producing HRR values over 100bpm.
    A steady 90bpm rhythm with exactly one such spike must not let that
    spike become the recovery reference point — HRR should stay small,
    reflecting the real (near-constant) rhythm, not the one outlier beat."""
    from app.ecg_analysis import compute_heart_rate_recovery

    sample_rate = 130
    peaks = [0]
    t_ms = 0.0
    # ~4 minutes of steady 90bpm...
    for _ in range(360):
        t_ms += 60000.0 / 90
        peaks.append(round(t_ms / 1000 * sample_rate))
    # ...with a single spurious 200bpm beat (300ms RR) injected partway through.
    spike_index = 180
    peaks[spike_index] = peaks[spike_index - 1] + round(0.300 * sample_rate)

    result = compute_heart_rate_recovery(peaks, sample_rate)
    assert result.hrr_1min is not None
    # A steady ~90bpm rhythm should show only mild variation, nowhere near
    # the 100+ bpm drop a single unsmoothed spike would have produced.
    assert abs(result.hrr_1min) < 20


def test_heart_rate_recovery_none_when_recording_too_short_past_peak() -> None:
    """A recording that ends right at (or shortly after) its peak HR can't
    measure a 1-2 minute recovery — must return None, not a fabricated
    number from whatever data happens to be at the end of the array."""
    from app.ecg_analysis import compute_heart_rate_recovery

    sample_rate = 130
    # 20s of steady 75bpm — a peak exists, but nothing extends a minute past it.
    peaks = list(range(0, sample_rate * 20, round(0.8 * sample_rate)))
    result = compute_heart_rate_recovery(peaks, sample_rate)
    assert result.hrr_1min is None
    assert result.hrr_2min is None


def test_heart_rate_recovery_none_for_too_few_peaks() -> None:
    from app.ecg_analysis import compute_heart_rate_recovery

    assert compute_heart_rate_recovery([0, 100], 130) == compute_heart_rate_recovery([], 130)


# ---------------------------------------------------------------------------
# HR-zone minutes (%HRmax training zones)
# ---------------------------------------------------------------------------


def _constant_hr_peaks(sample_rate: int, bpm: float, duration_s: float) -> list[int]:
    """Builds a peak list at a constant instantaneous HR — the RR interval
    stays fixed, so every gap attributes to the same zone by construction,
    making expected zone-minutes easy to state exactly."""
    rr_ms = 60000.0 / bpm
    n_beats = int(duration_s * 1000 / rr_ms) + 1
    peaks = [0]
    t_ms = 0.0
    for _ in range(n_beats):
        t_ms += rr_ms
        peaks.append(round(t_ms / 1000 * sample_rate))
    return peaks


def test_estimated_max_hr_is_the_mean_of_three_published_formulas() -> None:
    """Age 26 (2026 - ASSUMED_BIRTH_YEAR 2000): Fox (220-26=194),
    Tanaka (208-0.7*26=189.8), Gulati (206-0.88*26=183.12) — averaged, not
    just Fox et al. alone (see _estimated_max_hr's docstring for why)."""
    from datetime import date

    from app.ecg_analysis import _estimated_max_hr

    expected = (194.0 + 189.8 + 183.12) / 3.0
    assert _estimated_max_hr(today=date(2026, 1, 1)) == pytest.approx(expected)


def test_hr_zone_minutes_constant_hr_lands_in_expected_zone() -> None:
    """Age 26 (2026 - ASSUMED_BIRTH_YEAR 2000) -> max_hr = mean(Fox, Tanaka,
    Gulati) ≈ 188.97 (see test_estimated_max_hr_is_the_mean_of_three_
    published_formulas). Z2 is 60-70% ≈ 113.4-132.3bpm; 125bpm sits inside
    it, so ~1 minute of constant 125bpm HR should land almost entirely in
    zone2."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=125, duration_s=60)

    result = compute_hr_zone_minutes(peaks, sample_rate, today=date(2026, 1, 1))
    assert result is not None
    assert result.zone2 == pytest.approx(1.0, abs=0.05)
    assert result.zone1 == 0.0
    assert result.zone3 == 0.0
    assert result.zone4 == 0.0
    assert result.zone5 == 0.0
    assert result.below_zone1 == 0.0
    assert result.used_hrr is False


def test_hr_zone_minutes_below_zone1_for_resting_hr() -> None:
    """75bpm at max_hr≈188.97 (age 26, 2026) sits well under Z1's ≈94.5bpm
    floor (50% of max_hr) — should attribute entirely to below_zone1, the
    "rest between sets" bucket, not any of the 5 training zones."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=75, duration_s=60)

    result = compute_hr_zone_minutes(peaks, sample_rate, today=date(2026, 1, 1))
    assert result is not None
    assert result.below_zone1 == pytest.approx(1.0, abs=0.05)
    assert result.zone1 == 0.0
    assert result.used_hrr is False


def test_hr_zone_minutes_zone5_for_near_max_hr() -> None:
    """190bpm at max_hr≈188.97 (age 26, 2026) is >90% of max — zone5."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=190, duration_s=60)

    result = compute_hr_zone_minutes(peaks, sample_rate, today=date(2026, 1, 1))
    assert result is not None
    assert result.zone5 == pytest.approx(1.0, abs=0.05)
    assert result.used_hrr is False


def test_hr_zone_minutes_none_for_too_few_peaks() -> None:
    from app.ecg_analysis import compute_hr_zone_minutes

    assert compute_hr_zone_minutes([0, 100], 130) is None
    assert compute_hr_zone_minutes([], 130) is None


def test_hr_zone_minutes_sums_to_total_duration() -> None:
    """Every gap between consecutive valid RR readings must be attributed
    to exactly one bucket (a zone or below_zone1) — the six values should
    sum to the recording's total attributable duration, not lose or double-
    count any interval."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes, detect_r_peaks
    from tests.conftest import synthetic_ecg_samples

    sample_rate = 130
    samples = synthetic_ecg_samples(sample_rate, duration_s=90, bpm=140, seed=4)
    peaks = detect_r_peaks(samples, sample_rate)

    result = compute_hr_zone_minutes(peaks, sample_rate, today=date(2026, 1, 1))
    assert result is not None
    total = (
        result.zone1
        + result.zone2
        + result.zone3
        + result.zone4
        + result.zone5
        + result.below_zone1
    )
    # Should be close to the recording's nominal duration (90s = 1.5min),
    # modulo the RR plausibility filter dropping a few edge intervals.
    assert 1.0 < total < 1.6


def test_hr_zone_minutes_uses_karvonen_when_resting_hr_given() -> None:
    """max_hr≈188.97 (age 26, 2026), resting_hr=60 -> HRR≈128.97. %HRmax
    would put 130bpm in zone2 (60-70% of 188.97 ≈ 113.4-132.3), but under
    %HRR, 130bpm is (130-60)/128.97 ≈ 54.3% of HRR — inside zone1 (50-60%),
    not zone2. Confirms the two formulas actually diverge and that
    resting_hr being passed in is what selects Karvonen."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=130, duration_s=60)

    result = compute_hr_zone_minutes(peaks, sample_rate, resting_hr=60.0, today=date(2026, 1, 1))
    assert result is not None
    assert result.used_hrr is True
    assert result.zone1 == pytest.approx(1.0, abs=0.05)
    assert result.zone2 == 0.0


def test_hr_zone_minutes_karvonen_below_zone1_for_resting_hr() -> None:
    """A recording sitting right at its own resting_hr should attribute
    entirely to below_zone1 under %HRR too (0% of HRR is the floor by
    construction, same as %HRmax's near-zero floor)."""
    from datetime import date

    from app.ecg_analysis import compute_hr_zone_minutes

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=60, duration_s=60)

    result = compute_hr_zone_minutes(peaks, sample_rate, resting_hr=60.0, today=date(2026, 1, 1))
    assert result is not None
    assert result.used_hrr is True
    assert result.below_zone1 == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# Calorie burn / TRIMP (compute_calories_and_trimp)
# ---------------------------------------------------------------------------


def test_calories_and_trimp_matches_keytel_and_banister_by_hand() -> None:
    """Constant 130bpm for ~1 minute, age 26 (2026), weight 80kg, male,
    resting_hr=60: expected values computed by hand from the Keytel/Banister
    formulas (ANALYSIS_SPEC.md §2/§3), same max_hr as the other HR-zone
    tests (mean of Fox/Tanaka/Gulati ≈ 188.97) — not just "whatever the code
    outputs", per this file's own stated convention."""
    from datetime import date

    from app.ecg_analysis import compute_calories_and_trimp

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=130, duration_s=60)

    result = compute_calories_and_trimp(
        peaks,
        sample_rate,
        weight_kg=80.0,
        resting_hr=60.0,
        is_male=True,
        today=date(2026, 1, 1),
    )
    assert result is not None
    # kcal_per_min = (-55.0969 + 0.6309*130 + 0.1988*80 + 0.2017*26) / 4.184 ≈ 11.489
    # ~1 minute of constant HR -> total calories ≈ that per-minute rate.
    assert result.calories == pytest.approx(11.49, abs=0.1)
    # hrr_frac = (130-60)/(188.97-60) ≈ 0.5427
    # trimp_per_min = hrr_frac*0.64*exp(1.92*hrr_frac) ≈ 0.985
    assert result.trimp == pytest.approx(0.985, abs=0.02)


def test_calories_and_trimp_uses_female_coefficients_when_not_male() -> None:
    """Same constant-HR setup as the male test above, but is_male=False —
    must use the Keytel female coefficients and gender_exp=1.67 (Banister),
    not silently fall through to the male formula."""
    from datetime import date

    from app.ecg_analysis import compute_calories_and_trimp

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=130, duration_s=60)

    result = compute_calories_and_trimp(
        peaks,
        sample_rate,
        weight_kg=80.0,
        resting_hr=60.0,
        is_male=False,
        today=date(2026, 1, 1),
    )
    assert result is not None
    # kcal_per_min = (-20.4022 + 0.4472*130 - 0.1263*80 + 0.0740*26) / 4.184 ≈ 7.064
    assert result.calories == pytest.approx(7.06, abs=0.1)
    # trimp_per_min = hrr_frac*0.64*exp(1.67*hrr_frac), hrr_frac ≈ 0.5427
    assert result.trimp == pytest.approx(0.860, abs=0.02)


def test_calories_and_trimp_none_for_too_few_peaks() -> None:
    from app.ecg_analysis import compute_calories_and_trimp

    assert (
        compute_calories_and_trimp([0, 100], 130, weight_kg=80.0, resting_hr=60.0, is_male=True)
        is None
    )
    assert (
        compute_calories_and_trimp([], 130, weight_kg=80.0, resting_hr=60.0, is_male=True) is None
    )


def test_calories_and_trimp_trimp_clamped_to_zero_below_resting_hr() -> None:
    """A recording at/below resting_hr shouldn't produce negative TRIMP —
    hrr_frac is clamped to [0, 1], same convention as compute_hr_zone_minutes'
    below-zone1 bucket for sub-resting HR."""
    from datetime import date

    from app.ecg_analysis import compute_calories_and_trimp

    sample_rate = 130
    peaks = _constant_hr_peaks(sample_rate, bpm=55, duration_s=60)

    result = compute_calories_and_trimp(
        peaks,
        sample_rate,
        weight_kg=80.0,
        resting_hr=60.0,
        is_male=True,
        today=date(2026, 1, 1),
    )
    assert result is not None
    assert result.trimp == pytest.approx(0.0)
