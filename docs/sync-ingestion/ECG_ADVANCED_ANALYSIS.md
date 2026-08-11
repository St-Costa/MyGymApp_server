# Advanced ECG analysis — tier-1 metrics

Extends `docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md`'s ported R-peak/HRV/arrhythmia
pipeline (`app/ecg_analysis.py`) with a second wave of metrics chosen from a literature +
open-source review (see the review's own findings summarized below). Scope is deliberately
the "do first" tier only — high-confidence, well-supported in the literature, feasible now
against this project's actual data (single-lead chest-strap ECG recorded *during* gym
sessions, not at clinical rest, no labeled training data of the user's own).

## Why these five, and not others

The review covered three tiers: **do first** (this document), **worth prototyping**
(ECG-derived respiration, ensemble R-peak detection, AFib expert-feature mining, PVC/PAC
morphology CNNs, T-wave alternans), and **not recommended now** (QTc from single-lead,
ECG foundation models, training a bespoke classifier — all either unvalidated on
noisy/exercise single-lead data, or requiring labeled data this project doesn't have).
Only the first tier is implemented here; the others remain future work, not silently
dropped — see the review notes kept in the PR/commit history for the full tiered writeup.

## What's implemented

### 1. Signal Quality Index (SQI)

**Not `vital-sqi`** (the library the literature review pointed at): broken on PyPI as of
this writing — its `hrv-analysis` dependency imports `astropy.stats.LombScargle`, an API
astropy removed; pinning an old-enough astropy to restore it collides with numpy 1.x/2.x
across the rest of the dependency tree (including `neurokit2`, which needs numpy 2.x).
Rather than fight a broken, heavyweight (~80 transitive packages: librosa, statsmodels,
plotly, pyEDFlib, pycwt — none of which this project needs) dependency, SQI is computed
directly with two small, well-established indices:

- **kurtosis SQI (kSQI)**: kurtosis of the raw sample amplitude distribution over the
  recording. Clean QRS-dominated ECG has heavy-tailed, peaked amplitude statistics (high
  kurtosis); motion artifact/noise flattens the distribution. Computed with
  `scipy.stats.kurtosis` (already a `neurokit2` transitive dependency, not a new one).
- **bSQI (beat-detection agreement)**: fraction of beats our own `detect_r_peaks` (Pan-
  Tompkins + gap-filling, see `ECG_ANALYSIS_HANDOFF.md`) agrees on with an independent
  second detector (`py-ecg-detectors`'s Hamilton detector — different filter design,
  different peak-selection logic) within a small tolerance window. Two independently-built
  detectors disagreeing on a beat is itself strong evidence that beat is an artifact, not a
  real QRS — this is the same signal the handoff doc's "trailing max" fix and this
  project's gap-filling extension were both trying to protect against, made explicit and
  measurable instead of just hoped-for.

`b_sqi` is a `0.0-1.0` agreement fraction; `kurtosis_sqi` is *not* bounded (kurtosis has no
fixed range — values from under 1 to several thousand were observed across real recordings,
depending on how sharply peaked the amplitude distribution is) — treat it as a relative/
comparative signal (higher is generally cleaner), not a normalized score. No gating is
applied automatically yet — a low SQI is reported alongside the other metrics rather than
suppressing them, since "how low is too low to trust" needs real data from this project's
own recordings to calibrate, not a borrowed threshold from a different sensor/population.
Revisit once there's enough history to set an evidence-based cutoff.

**Bug found and fixed against real recordings (schema_version 3 → 4)**: `b_sqi`'s
tolerance-window comparison assumed the two detectors' peak positions would roughly line
up. In practice `py-ecg-detectors`' Hamilton detector runs a noticeably different filter
cascade than this module's own, producing a *systematic* (not random) phase delay of
anywhere from single digits to ~100 samples between the two peak streams — comparing raw
positions with only a small tolerance then flagged nearly every beat as "disagreement"
even on genuinely clean recordings (measured `b_sqi` values around 0.1-0.3 across the
board, regardless of actual signal quality). Fixed with `_estimate_phase_offset`: a
windowed histogram vote over every nearby (primary, secondary) peak pair, taking the mode
signed distance as the systematic delay before applying the tight tolerance. A naive
"each peak's single nearest neighbor" approach was tried first and rejected — it can
converge on the *wrong* side of a roughly-uniform RR spacing when the true offset exceeds
about half the local RR period (verified as a real failure mode, not just theoretical, via
a targeted regression test). After the fix, `b_sqi` on the same real recordings mostly
landed in the 0.6-0.9 range, consistent with what a clean signal should score.

### 2. DFA-α1 (short-term detrended fluctuation analysis exponent)

Fractal scaling exponent of the RR-interval series. Declines from ~1.0-1.5 at rest/easy
effort toward ~0.5 as exercise intensity rises; crossing **0.75** closely tracks the first
ventilatory/lactate threshold (VT1) in exercise-physiology literature (validated r≈0.97-0.99
against gas-exchange VT1 in incremental-exercise studies). Computed via
`neurokit2.hrv_nonlinear`'s `HRV_DFA_alpha1` output, fed the same RR-interval series
`compute_hrv_metrics`/`compute_poincare_metrics` already use — no separate implementation.

**Caveats carried into the stored value's interpretation** (not encoded in code, since
they're about *how a human reads the number*, not something the pipeline can enforce):
requires clean RR intervals — contaminated by frequent artifacts, the exponent is
unreliable, which is exactly what the SQI fields above are for cross-checking. Validated
on running/cycling incremental tests, not resistance training — most meaningful on a
session's cardio/warm-up segments, not during a set of heavy lifting with EMG noise and
static holds.

### 3. HRV frequency-domain metrics (LF, HF, LF/HF)

Standard autonomic-balance markers (Task Force 1996 guidelines): HF power tracks
parasympathetic/vagal activity, LF is mixed sympathetic-vagal, LF/HF a coarse
sympathovagal-balance proxy. Computed via `neurokit2.hrv_frequency` (Lomb-Scargle PSD
estimation under the hood — not reimplemented by hand here, matching this project's
general stance of using a maintained library for a well-defined statistical estimator
rather than hand-rolling one).

**Caveat**: needs several minutes of *stationary* RR data (HF: ~1 min minimum, LF: ~2 min,
LF/HF: ~5 min) to be meaningful — most defensible computed over a whole recording only when
that recording is long enough and reasonably homogeneous in intensity; a mixed
warm-up/lift/cardio/cooldown session will blend regimes the metric wasn't designed to mix.
Reported as-is (whole-recording), with this caveat, rather than attempting session-phase
segmentation, which the project doesn't have the input data for yet.

### 4. Nonlinear HRV extras (SampEn, CSI, CVI)

Sample entropy and Cardiac Sympathetic/Vagal Index, alongside SD1/SD2 (already computed by
the existing Poincaré port) — also from `neurokit2.hrv_nonlinear`, same call as DFA-α1
above, so these come "for free" alongside it rather than needing a separate pass over the
RR series.

**Performance cap — recent-window approximation, not whole-session**: `hrv_nonlinear`'s
SampEn estimator is O(n²) in RR-interval count. Measured at ~1.4s for 800 intervals but
100+ seconds by ~12,000 (an ordinary multi-hour gym session), which is unacceptable for
the synchronous in-request analysis path (`app.ingestion.ingest_ecg`). DFA-α1/SampEn/
CSI/CVI are therefore computed over only the **most recent 800 RR intervals**
(`_NONLINEAR_RR_CAP` in `app/ecg_analysis.py`), not the full session — an explicit,
documented approximation, not a silent truncation. LF/HF (§3) is *not* capped:
`hrv_frequency`'s PSD estimation is O(n log n) and stayed under 0.2s even on the
~12,000-point case, so there's no performance reason to sacrifice its whole-session
accuracy there.

### 5. Heart Rate Recovery (HRR1, HRR2)

Drop in heart rate at 1 and 2 minutes after peak effort — a well-validated
autonomic/parasympathetic-reactivation marker in cardiology literature (HRR1 ≤ 12 bpm
upright is the classic "abnormal" cutoff, independently prognostic across multiple
populations).

**Approximation used here, and why**: proper HRR needs a known "cessation of effort"
marker (end of a cardio interval, start of rest) — a gym session's raw ECG alone doesn't
carry that structure (no exercise/set timing is available to this pipeline; that lives in
the session's `.md` frontmatter, a separate sync record this analysis doesn't currently
join against). Rather than wait for that integration, `compute_heart_rate_recovery` uses
the recording's **peak HR** as an effort-cessation proxy, then measures the delta to HR at
1/2 minutes after that peak. This is a real approximation, not a clinical HRR
measurement — it can misfire on a session with multiple high-intensity intervals (only the
single highest peak is used) or no clear peak at all (e.g. a pure steady-state session).
Stored as `hrr_1min`/`hrr_2min` with this caveat; a future integration with session/set
timing (once ECG_SPEC.md-style joins against `sessions` exist) would let this become a
true multi-interval HRR rather than a single whole-recording approximation.

**Bug found and fixed against real recordings (schema_version 3 → 4)**: the peak search
originally used *unsmoothed* per-beat instantaneous HR (60000/rr_ms). Verified against
real gym-session ECGs, this let a single noisy-but-physiologically-plausible RR interval
(e.g. 300ms → 200bpm, inside the §2 300-2500ms filter but not a real sustained rate for a
gym session) become "the peak" on its own, producing HRR values over 100bpm — clinically
impossible. Fixed by smoothing instantaneous HR over a 12-second trailing window
(`_HR_SMOOTHING_WINDOW_S`) before searching for the peak and before measuring the
recovery delta: a real sustained effort period survives smoothing; a single anomalous
beat gets averaged away. Real HRR is always measured against a sustained rate in the
clinical literature too, never a single instant, so this isn't just noise reduction — it
matches how the metric is actually defined.

**Cardiac drift / aerobic decoupling**: reviewed and *not* implemented in this round — it
needs a genuine steady-state cardio segment (pace/power vs HR, first half vs second half)
that this pipeline has no way to identify automatically from ECG alone, and approximating
it the way HRR was approximated here would produce a number with no real interpretive
grounding (unlike HRR's peak-based proxy, there's no reasonable single-recording stand-in
for "a sustained steady effort segment"). Left as explicit future work pending
session-phase data, not silently dropped.

## Schema

`ecg_analysis` (see `app/storage.py`) gains:

```sql
ALTER TABLE ecg_analysis ADD COLUMN kurtosis_sqi   REAL;
ALTER TABLE ecg_analysis ADD COLUMN b_sqi          REAL;
ALTER TABLE ecg_analysis ADD COLUMN dfa_alpha1     REAL;
ALTER TABLE ecg_analysis ADD COLUMN hrv_lf         REAL;
ALTER TABLE ecg_analysis ADD COLUMN hrv_hf         REAL;
ALTER TABLE ecg_analysis ADD COLUMN hrv_lf_hf      REAL;
ALTER TABLE ecg_analysis ADD COLUMN sample_entropy REAL;
ALTER TABLE ecg_analysis ADD COLUMN csi            REAL;
ALTER TABLE ecg_analysis ADD COLUMN cvi            REAL;
ALTER TABLE ecg_analysis ADD COLUMN hrr_1min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hrr_2min       REAL;
```

All nullable, same "no result" convention as the existing twelve fields: `None`/`NULL`
when the underlying computation couldn't run (too little clean data, no RR series, no
detectable peak HR), never a misleading zero. `ECG_ANALYSIS_SCHEMA_VERSION` bumped
accordingly — existing rows are stale and due for re-analysis via `analyze_ecg.py --stale`.
