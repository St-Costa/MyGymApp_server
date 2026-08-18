# Advanced ECG analysis — tier-1 metrics

Extends `docs/sync-ingestion/ECG_ANALYSIS_HANDOFF.md`'s ported R-peak/HRV/arrhythmia
pipeline (`app/ecg_analysis.py`) with a second wave of metrics chosen from a literature +
open-source review (see the review's own findings summarized below). Scope is deliberately
the "do first" tier only — high-confidence, well-supported in the literature, feasible now
against this project's actual data (single-lead chest-strap ECG recorded *during* gym
sessions, not at clinical rest, no labeled training data of the user's own).

## Why these five, and not others

The review covered three tiers: **do first** (this document — implemented), **worth
prototyping** (tested one-by-one against real recordings and rejected — see "Tier 2 —
investigated and rejected" below), and **not recommended now** (never attempted — see
"Tier 3 — not recommended now" below). Nothing here is silently dropped; every tier is
tracked in this file.

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

**Discarded when HR rebounds instead of resting (schema_version 8 → 9)**: real recovery is
monotonically decreasing on the smoothed series — investigated against this project's real
data (August 2026) after noticing `hrr60s`/`hrr_1min`/`hrr_2min` running suspiciously low
for a trained subject, several near the clinical "abnormal" ≤12bpm cutoff, and `hrr_2min <
hrr_1min` (HR *worse* at 2min than at 1min — physiologically shouldn't happen in a real
passive recovery) in 16 of 32 sessions with both values present. Root cause: activity often
resumes well inside the 60s/120s window (next set/exercise starts, walking, talking) rather
than a genuine seated rest, especially during warm-up/daily-exercise blocks — so the
"recovery" delta is actually measuring against HR from renewed effort, not rest.
`_hr_recovery_at` now walks the smoothed-HR path from peak to the target offset and tracks
the running minimum; if HR climbs back more than `_HRR_REBOUND_TOLERANCE_BPM` (5bpm) off
that minimum before the target, the value is discarded (stored as NULL) rather than
reported — a rebound past 5bpm on an already-12s-smoothed series is a real change in
direction, not residual noise. `hrr_1min_discarded`/`hrr_2min_discarded` record *why* a
value is NULL (discarded vs. recording too short to measure at all), so this is auditable
over time rather than just quietly reducing coverage. This only touches `hrr_1min`/
`hrr_2min` (this pipeline's ECG-based approximation) — `sessions.hrr60s` is computed
phone-side from data this server never receives (per-set HR series with timestamps), so the
same check can't be applied there without a phone-side change; see
`PENDING_IMPROVEMENTS.md`.

**Not coalesced with `sessions.hrr60s` in the dashboard trend, despite both being "HRR" —
investigated and rejected**: `hrr60s` (ANALYSIS_SPEC.md §7) is the phone's own *mean of
multiple per-set recovery deltas* within a session (`peak_qualifies` gate + 90s debounce
between queued peaks), while `hrr_1min` here is a *single* delta from the recording's one
highest smoothed peak, per the whole-recording approximation described above — not the
same measurement, not "the same thing computed more accurately". Verified against this
project's real data before deciding: the two values diverge systematically, not just
noisily, by roughly +40% to +500% across 33 sessions with both values present (`hrr_1min`
consistently higher, since a single whole-session peak effort typically recovers less
steeply per-minute than the smaller, more frequent per-set peaks `hrr60s` averages). A
straight `COALESCE(hrr_1min, hrr60s)` — the pattern used for calories/TRIMP and VO2max
elsewhere in this codebase — would silently swap in a differently-defined number, not a
more-accurate version of the same one. Revisit only alongside the session/set-timing
integration mentioned above, which would let `compute_heart_rate_recovery` replicate the
phone's actual multi-peak/debounce logic against ECG data rather than approximating a
single whole-recording peak — at that point the two would be directly comparable.

**Cardiac drift / aerobic decoupling**: reviewed and *not* implemented in this round — it
needs a genuine steady-state cardio segment (pace/power vs HR, first half vs second half)
that this pipeline has no way to identify automatically from ECG alone, and approximating
it the way HRR was approximated here would produce a number with no real interpretive
grounding (unlike HRR's peak-based proxy, there's no reasonable single-recording stand-in
for "a sustained steady effort segment"). Left as explicit future work pending
session-phase data, not silently dropped.

### 6. Heart rate training zones (Z1-Z5, `compute_hr_zone_minutes`)

Minutes spent in each of 5 standard training zones (Z1 50-60% active recovery, Z2 60-70%
aerobic base, Z3 70-80% aerobic/tempo, Z4 80-90% threshold, Z5 90%+ maximal) plus
`hr_below_zone1_min` for time under even Z1's floor (rest between sets, not a training
zone). Attribution is per inter-beat gap, anchored to its leading beat's instantaneous HR.

**%HRR (Karvonen), not %HRmax, when a resting HR is available**: zone boundaries are
`resting_hr + pct*(max_hr - resting_hr)` rather than `pct*max_hr`. %HRmax treats two people
with the same max HR as equivalent regardless of cardiac reserve; %HRR corrects for it, and
is the more physiologically accurate method for someone training frequently with a low
resting HR (%HRmax would systematically under-report how hard such sessions actually are).
`compute_hr_zone_minutes` falls back to plain %HRmax when no resting HR is available for
the analyzed recording, and records which method was used in `hr_zones_used_hrr` (see
Schema below) so downstream consumers (the dashboard) know which formula produced a given
row rather than silently mixing the two.

**Resting HR input — 7-day trailing average, not same-day measurement**: `resting_hr` comes
from `app.analysis.resting_hr_7day_average`, a mean of `readiness_events.resting_hr` over
the 7 days ending on the recording's own session date (falling back to the sync-receipt
date if the session hasn't synced yet). Resting HR changes little day to day in a stable,
training athlete (roughly ±3-5bpm barring illness/acute stress), so a week-long average is
an acceptable substitute for days without a direct morning reading, rather than requiring
same-day data that often doesn't exist. Computed once per analyzed recording in
`app.ingestion.resting_hr_for_hr_zones` (the live `POST /v1/ecg` path) and mirrored in
`analyze_ecg.py` (the batch/backfill path) — both call the same `resting_hr_7day_average`,
per this project's one-parser-two-callers convention.

**Second fallback — the session's own `sessions.resting_hr`, when readiness_events has
nothing in the trailing window at all**: readiness tracking is a separate, newer feature
than session syncing, so it accumulates history independently — most sessions historically
have no `readiness_events` row anywhere near their date even when the phone's own
per-session `resting_hr` reading exists (`sessions.resting_hr` is populated on nearly every
synced session; `readiness_events.resting_hr` accumulates far more slowly). Rather than
falling all the way through to the %HRmax fallback (or skipping the calorie/TRIMP recompute
entirely — see §7 below) whenever readiness tracking simply hasn't caught up yet,
`resting_hr_for_hr_zones` reads `sessions.resting_hr` for that exact session as a second
choice: a real, same-day reading, just from a less-controlled measurement path than a
dedicated morning HRV reading (see `app.ingestion.resting_hr_for_hr_zones`'s own
docstring). Verified against this project's real data: this fallback alone raised %HRR/
calorie-TRIMP eligibility from 2/37 to 35/35 analyzed recordings with a matching session.

**Max HR is still age-based, not derived from this project's peak-HR data — same reasoning
regardless of %HRR vs %HRmax** (see below): switching the zone *formula* doesn't change
what's a safe *input* to it. Using an under-measured max HR would shift the whole %HRR
scale down (both numerator and denominator depend on it), so the accuracy of the underlying
max-HR estimate matters at least as much under Karvonen as it did under %HRmax.

**Max HR estimate — mean of three published age-based formulas, not one**: Fox et al. 1971
(220-age), Tanaka/Monahan/Seals 2001 (208-0.7×age, meta-analysis of 351 studies/~18,712
subjects, generally considered more accurate than Fox for older adults), and Gulati et al.
2010 (206-0.88×age, derived on ~5,437 women — included since Fox/Tanaka were both derived
on male-dominated samples and this app stores no sex). Averaging reduces reliance on any
one formula's specific coefficients; each has a standard error of estimate on the order of
±10-12bpm on its own.

**Explicitly not derived from this project's own ECG data, and why**: max HR can only be
*measured*, not estimated more accurately, via a controlled maximal/near-maximal graded
exercise test — a peak HR observed during a resistance-training session is not that, and
using it (or heart-rate-recovery/BMI/resting-HR derived from it) risks anchoring to a wrong
number. Verified concretely before deciding this: the highest instantaneous HR ever
observed across every real recording in this project was exactly 200bpm, on the one
recording already known to carry the most residual noise — landing precisely on
`_RR_MIN_MS`'s 300ms filter ceiling, not a real physiological peak. A literature review
(see the research this feature was built from) additionally found no published formula
uses resting HR, HRR, or HRV (RMSSD/SDNN) as an input to predict max HR at all — this
isn't a gap in this implementation, it doesn't exist to use. The one published multivariate
formula that adds BMI/body-fat%/fitness-level/VO2max (Sales et al. 2021, N=3,374, true
CPET) only improves R² marginally over age alone (0.19→0.22) and its own authors call it
"unlikely to be useful in clinical practice" — and even that marginal formula requires a
true maximal-test-derived VO2max as input, which this pipeline has no way to supply.
Age-only, averaged across three formulas, is the defensible choice given only passively-
collected submaximal gym-session data.

Age is derived from `ASSUMED_BIRTH_YEAR` (`app.config`) — see that constant's own
docstring; recomputed from the current date on every call, not cached, so it drifts
forward automatically as years pass.

### 7. Calorie burn / TRIMP recompute (`compute_calories_and_trimp`)

The phone already computes `sessionCalories` (Keytel et al. 2005) and `sessionTrimp`
(Banister exponential, gender-weighted — see `ANALYSIS_SPEC.md` §2/§3) incrementally,
per-sample, off its own optical wrist/chest-strap HR sensor and a static profile weight.
`compute_calories_and_trimp` recomputes the *same two formulas* server-side, from the
ECG's real beat-by-beat instantaneous HR (`_instantaneous_hr_series`, the same series
`compute_hr_zone_minutes`/`compute_heart_rate_recovery` use) instead of the phone's
optical sensor, and a scale weigh-in close to the session date instead of a static
profile value. Only the *inputs* change — the formulas are transcribed unchanged from
the spec, not redesigned.

**Why this is more accurate, not just "a second opinion"**: an optical HR sensor is
materially noisier than ECG during resistance training specifically (motion artifact,
poor skin contact from gripping a bar, etc.) — R-peak detection off the raw ECG waveform
doesn't have that failure mode. Weight is looked up per-session from the nearest scale
weigh-in (`app.analysis.nearest_scale_weight_kg`, within a 14-day window) rather than
whatever static value the phone's profile happened to hold at the time.

**Inputs and where they come from** (both required — see "no result" below if either is
missing):
- `resting_hr`: `app.ingestion.resting_hr_for_hr_zones` — the same lookup (7-day
  readiness_events average, falling back to the session's own sessions.resting_hr) already
  used for %HRR zone boundaries — reused rather than computed twice, since both are
  zone-relative calculations off the same baseline. See §6 above for the fallback's
  rationale/impact.
- `weight_kg`: `app.analysis.nearest_scale_weight_kg` — the closest scale weigh-in
  (before or after) to the session's date, within `_NEAREST_WEIGHT_MAX_DAYS` (14 days).
  Beyond that window body weight may have moved enough that using it risks being a worse
  input than not recomputing at all, so it's treated as absent instead.
- `max_hr`: `_estimated_max_hr` — the same Fox/Tanaka/Gulati average as the HR-zone
  feature (§6), not the phone's Tanaka-only formula. This is a deliberate divergence
  from `ANALYSIS_SPEC.md` §4.1's phone-side reference formula, for the same reasoning
  documented in §6 above — one max-HR estimate for both features, not two.
- `is_male`: defaults `True` — this app has no stored sex, same documented gap as
  `_estimated_max_hr`'s Gulati-inclusion decision.

**"No result" when inputs are missing**: `session_calories_ecg`/`session_trimp_ecg` are
left `NULL` (not a value computed from a guessed weight/resting HR) whenever either
`resting_hr` or `weight_kg` can't be resolved for a session — e.g. no readiness reading
in the trailing week, no scale weigh-in within two weeks of the session date, or (for
very early adopters of readiness/scale tracking) no history at all yet.

**Where these values are surfaced**: `app.analysis._session_metric_trend` (backing
`session_calories_trend`/`session_trimp_trend`, the dashboard's Training-load charts)
prefers `ecg_analysis.session_calories_ecg`/`session_trimp_ecg` over `sessions.
session_calories`/`session_trimp` via `COALESCE`, falling back to the phone's own value
whenever the ECG recompute is unavailable (no ECG synced for that session, or missing
resting-HR/weight inputs) — a session's plotted point never disappears just because the
recompute couldn't run.

**Rejected: a MET-based correction for resistance-training blocks.** Keytel/TRIMP are
both validated against cardio steady-state effort, not resistance training — HR rises
during a heavy set for reasons that aren't purely metabolic (isometric strain, Valsalva,
acute stress), so applying a pure HR-based formula across a whole session risks over/
under-estimating the strength-training portions specifically. A Compendium-of-Physical-
Activities MET value (~6.0 for vigorous resistance training) applied during `forza`-typed
sets was prototyped and rejected: `exercise_sets.started_at`/`ended_at`/`avg_hr`/`max_hr`
are currently populated only for `CARDIO`-typed blocks (`CARDIO_BLOCKS_SPEC.md`), not
`forza`/`stretch` — so there's no way to know *when* within a recording a strength set
actually happened versus rest between sets. A coarse proxy (session-wide fraction of
`forza` sets, distributed evenly across the recording) was tested against real data and
produced unstable, physiologically-meaningless swings (-8% to +43% versus plain Keytel
across sessions with near-identical composition) — worse than not correcting at all.
Revisit this once per-set timing exists for `forza`/`stretch` blocks too, not before.

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
ALTER TABLE ecg_analysis ADD COLUMN hr_zone1_min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_zone2_min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_zone3_min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_zone4_min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_zone5_min       REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_below_zone1_min REAL;
ALTER TABLE ecg_analysis ADD COLUMN hr_zones_used_hrr    INTEGER;
ALTER TABLE ecg_analysis ADD COLUMN session_calories_ecg REAL;
ALTER TABLE ecg_analysis ADD COLUMN session_trimp_ecg    REAL;
```

All nullable, same "no result" convention as the existing twelve fields: `None`/`NULL`
when the underlying computation couldn't run (too little clean data, no RR series, no
detectable peak HR), never a misleading zero. `hr_zones_used_hrr` is 0/1/NULL: which
formula (%HRR vs %HRmax fallback) computed the zone columns on that row — see "%HRR
(Karvonen)" above. `session_calories_ecg`/`session_trimp_ecg` are `NULL` whenever
`resting_hr`/`weight_kg` couldn't be resolved for that session — see §7 above.
`ECG_ANALYSIS_SCHEMA_VERSION` bumped accordingly — existing rows are stale and due for
re-analysis via `analyze_ecg.py --stale`.

## Tier 2 — investigated and rejected

The literature review's "worth prototyping" tier was tested one-by-one against the ~40
real recordings in this project's own database (not synthetic signals) before deciding
whether to build any of it for real. All five were rejected — documented here so this
isn't silently re-investigated later. Prototype scripts aren't kept in the repo (throwaway,
run from the scratchpad); the numbers below are what they produced.

**1. ECG-derived respiration (EDR/RSA)** — `neurokit2.ecg_rsp` (vangent2019 method).
Rejected: estimated breathing rate settled at a near-constant **14-16 breaths/min across
entire 2-hour recordings**, regardless of what the session actually involved (warm-up,
heavy sets, cardio, rest) — the 0.1-0.4Hz bandpass filter is producing a quasi-periodic
artifact of its own, not tracking a real respiratory signal, on this noisy exercise-context
single-lead data. Confirms neurokit2's own docstring warning ("help is required to
double-check whether the implementation match the papers").

**2. Ensemble R-peak detection** (voting/correction, beyond the bSQI cross-check already
in production) — compared our detector against `py-ecg-detectors`' Hamilton and Christov
detectors. Rejected: on clean recordings (high `b_sqi`) all three detectors already agree
to within <1% — nothing to gain from voting. On recordings where they *do* diverge
(e.g. `00b20ee4`: 21205 vs 15732 vs 21232 peaks — Hamilton found 26% fewer), the
disagreement is too large to trust an automatic vote to pick the right answer rather than
just swapping one unreliable estimate for another. `b_sqi` (already implemented) already
surfaces exactly this signal — an ensemble-correction layer wouldn't add anything a human
reading `b_sqi` doesn't already know.

**3. AFib expert features (ENCASE-style)** — computed RR-diff Shannon entropy, global
coefficient of variation, and % of RR intervals >20% from the local median, alongside the
existing `afib_suspicion_episodes`. Rejected: `pct_irregular` correlated **r=0.95** with
the existing metric across all 36 usable recordings — almost entirely redundant, not new
signal. `rr_entropy` was noisy and sensitive to recording length/fixed bin count, unusable
without real normalization work. `global_cv` (r=0.75) captured something partially
different but, being a single whole-recording statistic, is strictly less informative than
the existing sliding-window detector, which already localizes *when* in the session
irregularity happens.

**4. PVC/PAC morphology clustering** — extracted per-beat templates via
`biosppy.signals.ecg.extract_heartbeats` (fed our own detected peaks, not biosppy's own
segmenter) and ran k-means (k=3) on z-score-normalized template shapes. Rejected: flagged
**20-66% of beats as "minority" morphology** across recordings — far too high to be real
PVC/PAC (clinically rare, typically <5% even in patients with meaningful arrhythmia burden).
Manual inspection of one case (`22abda6d`, 19.4% minority) showed the "different" cluster
was just lower-amplitude beats (mean peak 2.97 vs ~4.5 for the other two, nearly identical
in shape) — consistent with degraded electrode contact during movement, not distinct beat
morphology. No ground truth available to validate whether any cluster corresponds to a real
PVC/PAC, and no reason to trust k-means-with-fixed-k to make that distinction on its own.
(Also worth flagging: `biosppy` doesn't correctly declare its `peakutils` dependency —
`import biosppy` fails outright until `peakutils` is installed separately. Not disqualifying
on its own, but a data point alongside `vital-sqi`'s breakage when weighing "how maintained
is this library" for any future revisit.)

**5. T-wave alternans (TWA)** — used `neurokit2.ecg_delineate` (DWT method) to locate T-wave
peaks, then checked whether an even/odd-beat amplitude alternation was measurable at all
before attempting a real TWA implementation (there's no ready-made TWA function in
neurokit2; building one means implementing the spectral/statistical method from scratch).
Not rejected on "it doesn't work" — T-peak delineation returned a value for essentially
every beat, including on recordings/windows already known to carry motion artifact, which
is itself the concerning finding: a near-100% "success" rate on noisy data suggests the
delineator returns *something* even when the true T-wave isn't reliably identifiable, i.e.
silent false positives, not genuine robustness. Without labeled/reference data there's no
way to tell real alternans from residual noise passed through the same detector, and the
literature itself already flags ambulatory/exercise TWA as lacking standardized detection
thresholds (see "Tier 3" below). Left as explicitly not-attempted beyond this delineation
sanity-check — building the full spectral method would be speculative effort with no way
to validate the result.

## Tier 3 — not recommended now

From the original literature review, never attempted (not even prototyped) — each has a
concrete blocker specific to this project's data (single-lead chest-strap ECG recorded
during resistance exercise, single user, no labeled training data of their own), not a
general "this technique doesn't work" judgment. Revisit if the blocker changes (e.g. once
months of self-labeled data accumulate).

**QTc estimation from single-lead ECG** — QTNet (deep learning, Lead-I, reported MAE~12ms,
r=0.91 in the original paper) is real and was validated reasonably well, but against
*clinical rest* 12-lead-derived Lead-I data, not noisy exercise chest-strap recordings, and
no public weights/repo were found (paper-only as far as the literature review surfaced).
T-wave detection itself is already unreliable under motion artifact before QT measurement
is even attempted (see Tier 2's TWA finding above) — high risk of a confidently-wrong
number with no way to catch the error.

**Full ECG foundation models** (ECG-FM, ECGFounder, ST-MEM, DeepECG-SSL) — real, open-weight,
permissively-licensed, genuinely strong 2024-2025 releases (e.g. `PKUDigitalHealth/ECGFounder`
on GitHub, weights on Hugging Face, 150-class output). Blocked because every one of these is
pretrained on 12-lead (or at minimum clean ambulatory) clinical datasets (MIMIC-IV-ECG,
PTB-XL-scale corpora) — none documented as validated on single-lead chest-strap data
collected *during resistance exercise*. Running one and trusting its output on this
project's noisy signal without any of its own validation data would be exactly the
"confidently wrong, hard to audit" failure mode this project's engineering discipline
should avoid (see Tier 2's PVC/PAC and TWA findings — the same class of risk played out
concretely there). Reasonable as a longer-term experiment (fine-tune on this project's own
weakly-labeled sessions once enough accumulate, using the existing heuristics as weak
labels), not now.

**Training a bespoke arrhythmia/PVC classifier from scratch** (e.g. on MIT-BIH) — feasible
technically, but this project has no labeled data of its own, and public databases
(MIT-BIH, CPSC-2020) are the wrong noise domain (Holter/clinical, not barbell-and-motion
noise) — a model trained purely on them risks being confidently wrong on this project's
actual signal with no validation set to catch it. Don't build this until a self-labeled
corpus exists (e.g. sessions where flagged episodes from the existing heuristics are
manually confirmed/corrected).

**TinyML / on-device quantized models** (heartKIT, ArrythML, MobileNet-compressed variants) —
solves compute-constrained *edge* inference, which is irrelevant here: the whole motivation
for server-side analysis (see `ECG_ANALYSIS_HANDOFF.md`) is that the server has compute the
phone never did. Not worth the added complexity of quantization/TFLite conversion on a
server that can run full-precision models.
