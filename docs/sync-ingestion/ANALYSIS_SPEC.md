# MyGymApp server — data analysis logic (port from phone to server)

> **Update (post-`isBodyweight` fix)**: the phone-side app now sends `isBodyweight: true`
> on `exercise_sets` for genuinely bodyweight work (weight=0 by design — plank, push-ups),
> distinct from a set that was simply never touched (also weight=0, no `isBodyweight`
> flag). Bump `PARSER_SCHEMA_VERSION`, add `is_bodyweight` to `exercise_sets`, and update
> §1 below's tonnage/PR/e1RM logic to treat `weight=0 AND is_bodyweight=True` as valid
> completed work (tonnage correctly evaluates to 0, but the set is *not* filtered out as
> incomplete) rather than silently dropping it via a `weight > 0` filter. `reparse.py --all`
> recovers pre-existing raw files once this lands, since they already have the field if
> sent after this change — the field was simply never present before it, which parses fine
> under the existing "missing field defaults to false" tolerance.

## Context

The MyGymApp Android app currently computes a set of derived metrics phone-side —
tonnage, calorie burn, TRIMP, VO2max, HRV readiness, ECG-derived HRV/arrhythmia markers,
recovery state, body composition, and trend aggregations. The goal now is to **move this
analysis to the server**, running against the synced raw data already landing in
`sessions` / `readiness_events` / `scale_weighins` (see `SPEC.md`, `READINESS_SPEC.md`,
`SCALE_SPEC.md`).

This document is a complete, formula-exact inventory of every computation currently done
on the phone, extracted directly from the Kotlin source. Every formula below is
transcribed verbatim — implement server-side exactly as written unless you have a
deliberate reason to change something (and if so, note the change explicitly rather than
silently drifting from the phone's behavior, since the phone may still compute some of
these values independently for a while).

**Important**: some inputs (7-day resting-HR baseline, 14-day LnRMSSD baseline) currently
live in Android `SharedPreferences`, not in synced files — they are *not* directly
available server-side from the raw session/readiness data alone. Where this applies, it's
called out explicitly with a suggested reconstruction approach (recompute from the
`readiness_events`/`sessions` history you already have, since the raw values that feed
those baselines — `restingHr`, `lnRmssd` per event — are present in each stored record).

---

## Already computed phone-side and sent as-is (no server logic needed)

These arrive pre-computed in the session/readiness YAML and just need to be stored/
queried — **do not recompute them**, they're already correct in the synced data:

- `totalTonnage`, `tonnageByBodypart` (session)
- `sessionCalories`, `sessionTrimp`, `vo2max` (session)
- `ecgBeats`, `ecgDurationSec`, `ecgAvgHr`, `ecgSessionRmssd`, `ecgPacCount`,
  `ecgPauseCount`, `ecgIrregularBeats`, `sdnn`, `pnn50`, `poincareSd1`, `poincareSd2`,
  `poincareRatio`, `afibSuspicionEpisodes`, `cardiacDriftBpmMin`, `restingHr`, `hrr60s`
  (session — all ECG/HRV metrics, already computed by the phone's offline ECG analyzer)
- `readiness`, `lnRmssd`, `restingHr`, `vo2max`, `recommendation` (readiness event)
- `weightKg`, `bmi`, `bodyFatPercent`, `leanMassPercent` (scale weigh-in)

What's below is everything **derived from these stored values** — the trend/aggregate/
comparison layer that today only exists transiently in the phone's UI (recomputed on
every screen open, never persisted) and should move server-side so it can run once and be
queried repeatedly, and so the weekly analysis job can build on it.

---

## 1. Tonnage & volume analysis

### 1.1 Session tonnage (already computed, stored as-is — see above)

### 1.2 Common-tonnage comparison between two sessions
Used to compare "this session vs the previous one for the same routine" without a
sudden jump/drop just because an exercise was added/removed.

```
def common_tonnage(s1, s2, exercises1, exercises2):
    # exercises1/2: this session's exercise list (exerciseId, excludeFromTonnage,
    # isDaily, completed, sets — i.e. WorkoutExercise-equivalent)
    def eligible(exercises):
        return {e.exercise_id for e in exercises
                if not e.exclude_from_tonnage
                and not is_untouched(e)          # completed=False and sets=[]
                and not e.completed_empty}        # completed with nothing touched

    common_ids = eligible(exercises1) & eligible(exercises2)
    if not common_ids:
        return (s1.total_tonnage, s2.total_tonnage)   # fallback: no overlap

    def tonnage_for(exercises, common_ids):
        return sum(
            set.reps * set.weight
            for e in exercises
            if e.exercise_id in common_ids and not e.exclude_from_tonnage
            for set in e.sets
        )
    return (tonnage_for(exercises1, common_ids), tonnage_for(exercises2, common_ids))
```

**Known quirk to preserve or deliberately fix**: the intersection (`common_ids`) is built
using three exclusion predicates (`excludeFromTonnage`, untouched, completedEmpty), but
`tonnage_for` only re-checks `excludeFromTonnage` when summing. An untouched/completedEmpty
exercise that still ends up in `common_ids` contributes 0 anyway (its `sets` list is
empty), so in practice this is harmless — but replicate the same two-predicate structure
rather than "simplifying" it, in case there's an edge case the original didn't hit.

**Tonnage change %**: `(curr - prev) / prev * 100.0`, undefined/null if no previous
session or `prev <= 0`.

### 1.3 Per-exercise change % (within a session vs the prior one)
```
prev_ex_tonnage = sum(reps*weight for sets of that exercise in the previous session,
                       excludeFromTonnage filtered out)
curr_ex_tonnage = sum(reps*weight for sets of that exercise in this session)
change_pct = (curr_ex_tonnage - prev_ex_tonnage) / prev_ex_tonnage * 100   # only if prev > 0
is_first_time = exercise_id was in previous session's map with 0 tonnage (or absent)
                AND curr_ex_tonnage > 0
```
Only meaningful for FORZA exercises with `!excludeFromTonnage` and not completed-empty.

### 1.4 Estimated 1RM — Epley formula
```
estimate_1rm(weight, reps) = weight * (1 + reps / 30.0)

best_estimated_1rm(sets) = max(estimate_1rm(s.weight, s.reps)
                                for s in sets if s.reps > 0 and s.weight > 0)
                            # None/null if sets is empty or all zero
```
**Per-session best e1RM** = the **max** (not sum) of `best_estimated_1rm(ex.sets)` across
all exercises in the session, 0.0 if none qualify.

**e1RM change %**: `(curr_best_e1rm - prev_best_e1rm) / prev_best_e1rm * 100`, only if
`prev > 0` and `curr` is non-null.

### 1.5 PR (personal record) detection — two DIFFERENT definitions, both used

**All-time tonnage PR** (single best set, ever):
```
candidates = all Strength sets across every session ever containing this exercise_id,
             from exercise entries with matching exercise_id and NOT excludeFromTonnage,
             where reps > 0 and weight > 0
pr = argmax(reps * weight over candidates)   -> (reps, weight) of the winning set
```

**Recent heaviest-set PR** (last 30 sessions, weight-first):
```
sessions = last 30 sessions containing the exercise, matched like-for-like on:
    is_daily == this_occurrence.is_daily
    AND (exclude_from_tonnage and not is_daily) == this_occurrence.is_warmup
best_set = max(sets with weight > 0, key = (weight, reps))   # lexicographic: weight first
```
Keep these as two distinct queries/outputs if reimplementing both badges — do not merge
them into one "PR" concept, the app deliberately shows different numbers in different
places using different criteria.

### 1.6 Trend series windowing conventions (for charts/weekly analysis)

- **Gitgraph / day-by-day comparison**: 28-day window (4 ISO weeks), `today_index` = current
  weekday's cell in the last row.
- **Session tonnage/e1RM trend**: **12-week window**, same `routineId`, `completedAt`
  non-blank, chronological.
- **Cardio 4-week card**: 28-day window split into `recent` (last 14 days) and `baseline`
  (14–28 days ago) halves — see §5.1.
- **Long-horizon cardio charts**: 6-month fetch window; weekly buckets for
  restingHR/HRR (8-week rolling), 4-week buckets for VO2max (over the full 6 months).
- **Scale weight trend**: 90-day fetch window, 8-week rolling weekly-median buckets.

---

## 2. Calorie burn — Keytel et al. (2005)

Computed **incrementally per HR sample** on the phone (elapsed-time-weighted). For a
server-side recompute from a session's aggregate data, you likely only have the *result*
(`sessionCalories`) already stored — this formula is here for reference/validation, and in
case you ever want to recompute from a raw per-second HR series if that's ever synced too.

```
elapsed_min = seconds_since_last_sample / 60.0
if elapsed_min <= 0 or elapsed_min > 0.2: skip this sample (gap > 12s)

if male:
    kcal_per_min = (-55.0969 + 0.6309*HR + 0.1988*weight_kg + 0.2017*age) / 4.184
else:
    kcal_per_min = (-20.4022 + 0.4472*HR - 0.1263*weight_kg + 0.0740*age) / 4.184

if kcal_per_min > 0:
    session_calories += kcal_per_min * elapsed_min
```
Inputs: instantaneous HR (bpm), user's `weightKg`, `age`, `isMale`.

---

## 3. TRIMP (Training Impulse) — Banister exponential, gender-weighted

Same incremental-per-sample computation, for reference:
```
hrr = (HR - resting_hr) / (hr_max - resting_hr)      # HR reserve fraction
hrr = clamp(hrr, 0.0, 1.0)
gender_exp = 1.92 if male else 1.67
trimp_contribution = elapsed_min * hrr * 0.64 * exp(gender_exp * hrr)
session_trimp += trimp_contribution
```
Same `elapsed_min` gate as calories. `hr_max` from Tanaka formula (§4.1).

---

## 4. HRmax and VO2max

### 4.1 HRmax — Tanaka formula
```
hr_max = int(208 - 0.7 * age)
```

### 4.2 VO2max — Uth-Sørensen-Overgaard-Pedersen formula
```
vo2max = 15.3 * (hr_max / hr_rest_for_vo2)
```
where `hr_rest_for_vo2` = **min of the last 7 daily resting-HR readings** (not just
today's). This is the one place a rolling baseline matters for VO2max:

**Server-side reconstruction**: query the last 7 `readiness_events` (or sessions with a
non-zero `restingHr`) for this user, take `min(restingHr)` across them, and recompute
`vo2max` with that. If fewer than 7 exist, use whatever is available (the phone falls back
to "just today's value" when the baseline is empty — same rule applies: `min` over
however many are actually available).

---

## 5. HRV Readiness (LnRMSSD z-score) — already computed and stored, formulas for reference

The `readiness` classification, `lnRmssd`, `restingHr`, `vo2max` all arrive pre-computed
in each `readiness_events` row. These formulas are here so the weekly analysis job can
**recompute a corrected/extended readiness trend** (e.g. re-classify using a longer
baseline than the phone's fixed 14-day window) rather than just re-deriving what's already
stored.

### 5.1 RMSSD / LnRMSSD
```
RMSSD = sqrt(mean((rr[i+1] - rr[i])^2 for consecutive RR pairs))   # 0.0 if <2 intervals
ln_rmssd = ln(RMSSD) if RMSSD > 0 else 0.0
```

### 5.2 Artifact filtering (applied to raw RR before RMSSD, phone-side already)
```
filtered = [rr for rr in rr_intervals if 300 <= rr <= 2000]        # ms
if len(filtered) < 3: return filtered
median = sorted(filtered)[len // 2]
return [rr for rr in filtered if abs(rr - median) < median * 0.20]  # within ±20% of median
```

### 5.3 Baseline & z-score
The phone's baseline is the **trailing 14 LnRMSSD values, excluding today's own** — stored
in `SharedPreferences`, not synced. Server-side, reconstruct from the last 14
`readiness_events.ln_rmssd` values (excluding the one being classified):
```
mean = average(baseline)
sd   = sqrt(mean((x - mean)^2 for x in baseline))    # population SD, not sample SD
z_score = (ln_rmssd - mean) / sd   if sd > 0 else 0.0
```
Requires **at least 7** baseline values to classify (fewer → `NO_BASELINE`).

### 5.4 Classification cutoffs — evaluate in this exact order
| Condition | Readiness | Recommendation text |
|---|---|---|
| `z < -1.5` | `DELOAD_RECOMMENDED` | "HRV significantly below baseline. Consider rest or light session." |
| `-1.5 ≤ z < -1.0` | `LIGHT_DAY` | "HRV moderately suppressed. Reduce volume or intensity by 20%." |
| `-1.0 ≤ z < 1.0` | `NORMAL` | "HRV within normal range. Proceed with planned workout." |
| `z > 1.5` | `PEAK` | "HRV unusually high. Consider testing a PR." |
| else (`1.0 ≤ z ≤ 1.5`) | `GOOD` | "HRV above baseline. Good day to push intensity." |

---

## 6. Offline ECG analysis — already computed and stored, formulas for reference/validation

All of §6 arrives pre-computed in each session's ECG/HRV fields. Included here only so the
server-side analysis job can validate/cross-check or recompute if you ever decide to sync
raw ECG too (not currently planned). **Skip implementing this unless you specifically want
to reprocess raw ECG server-side** — normally just consume the stored fields.

<details>
<summary>Pan-Tompkins QRS detection + HRV/arrhythmia formulas (click to expand if needed)</summary>

Sample rate: 130 Hz (Polar H10).

**Pipeline**: low-pass (window `sr/15`) → high-pass (subtract low-pass with window `sr/5`)
→ 5-point derivative → square → integrate (window `0.150*sr`) → adaptive threshold using a
**5-second trailing max** (`threshold = trailing_max * 0.5`), refractory period `0.25*sr`.

**RR-derived metrics**:
```
avg_hr = 60000.0 / mean(rr)
session_rmssd = sqrt(mean((rr[i+1]-rr[i])^2))
mean_rr = mean(rr)
sdnn = sqrt(mean((rr - mean_rr)^2))
nn50 = count(|rr[i+1]-rr[i]| > 50ms for consecutive pairs)
pnn50 = nn50 / num_pairs * 100
```

**Poincaré**:
```
msd = mean((rr[i+1]-rr[i])^2)
sd1 = sqrt(0.5) * sqrt(msd)
sd2 = sqrt(2*sdnn^2 - 0.5*msd)   # 0 if negative under the sqrt
ratio = sd2/sd1 if sd1 > 0 else 0.0
```

**Arrhythmia markers** (sliding local median, window 10, single pass):
- **Pause**: `rr > 2000ms`.
- **PAC**: `rr < local_median*0.85` AND next `rr > local_median*1.10` (short beat + compensatory pause).
- **Irregular beat**: `rr > local_median` AND `|rr - local_median| > local_median*0.20`,
  counted only when **two consecutive** such beats occur (counted as 2 when confirmed).

**AFib suspicion episodes**: sliding 30-RR windows (50% overlap, step 15).
```
cv = stddev(window) / mean(window)
autocorr = lag1_covariance(window) / variance(window)
irregular_window = (cv > 0.12) and (abs(autocorr) < 0.20)
```
Count rising edges of `irregular_window` (a contiguous run = 1 episode).

</details>

---

## 7. Heart Rate Recovery (HRR60s) — already computed and stored, formula for reference

```
peak_qualifies = (peak_hr - resting_hr >= 25) and (peak_hr >= hr_max * 0.6)
# debounce: only queue a new peak if >90s since the last queued one
delta = peak_hr - hr_sample_taken_at_or_after(peak_time + 60s)   # not interpolated
hrr60s = mean(all deltas in the session)   # 0.0 if none
```

---

## 8. Cardiac drift — already computed and stored, formula for reference

Plain OLS (least-squares) linear regression slope of HR vs elapsed time:
```
if fewer than 60 HR samples, or session < 5 minutes: drift = 0.0
xs = elapsed_minutes for each sample
ys = hr for each sample
slope = sum((x - mean(xs)) * (y - mean(ys))) / sum((x - mean(xs))^2)
cardiac_drift_bpm_min = slope    # BPM per minute; positive = HR trending up over the session
```

---

## 9. Recovery state semaphore (RECOVERING / ALMOST_READY / READY)

This is a **live, in-workout** state (updated per HR sample during an active session) —
not something that needs to exist server-side unless you want to reconstruct "how much of
the session was spent recovering" as a retrospective metric. Included for completeness;
likely **not worth porting** unless you have a specific use for it.

```
# Peak detection: rolling 8-sample HR window, rising→falling edge with peak-to-rest rise >= 15 BPM
# State, once a peak is being tracked:
hr_delta = peak_hr - resting_hr
hr_ready = ((peak_hr - current_hr) / hr_delta) >= 0.70   if hr_delta > 0 else True
rmssd_ready = rmssd(recent_30_rr) > 20.0 and len(recent_rr) >= 15

state = READY        if hr_ready and rmssd_ready
      = ALMOST_READY if hr_ready or  rmssd_ready
      = RECOVERING   otherwise
```

---

## 10. Body composition (BIA) — already computed and stored, formula for reference

Deurenberg-family regression, from weight + height + age + sex + bioimpedance:
```
bmi = weight_kg / (height_cm/100)^2

# sex-dependent coefficients:
age_factor = 0.103 if male else 0.097
bmi_factor = 1.524 if male else 1.545
constant   = 22.0  if male else 12.7

raw_fat_percent = age_factor*age + bmi_factor*bmi - 500.0/impedance_ohm - constant
body_fat_percent = clamp(raw_fat_percent, 5.0, 75.0)
lean_mass_percent = 100.0 - body_fat_percent
```

---

## 11. Trend aggregation — THE MAIN THING TO PORT SERVER-SIDE

This is the part that's genuinely worth building server-side, since it's currently
recomputed on-demand phone-side every time a screen opens rather than being a stored,
queryable result. Recommend implementing these as the actual "weekly analysis" job's core
output.

### 11.1 Cardio 4-week trend card (semaphore-based)

Window: last 28 days, split into `recent` (last 14 days) vs `baseline` (14–28 days ago).
`has_enough_data = len(recent_sessions) >= 2`.

Per metric:
```
current = mean(recent_values) or 0.0
baseline_avg = mean(baseline_values) or current
delta = current - baseline_avg

direction = FLAT if current == 0.0
          = FLAT if abs(delta) < abs(baseline_avg) * 0.02      # ±2% dead zone
          = UP   if delta > 0 else DOWN

semaphore = GRAY   if current == 0.0
          = RED    if absolute_bad(current)
          = RED    if good_direction != FLAT and direction != FLAT and direction != good_direction
                      and abs(delta) > abs(baseline_avg) * 0.10
          = YELLOW if good_direction != FLAT and direction != FLAT and direction != good_direction
          = GREEN  if absolute_ok(current)
          = YELLOW otherwise
```

Metric table (field, which direction counts as "good," absolute red/green cutoffs):

| metric | source field | good direction | RED if | GREEN if |
|---|---|---|---|---|
| restingHr | `restingHr` (>0) | DOWN | `>85` | `40..75` |
| hrr60s | `hrr60s` (>0) | UP | `<12` | `>=20` |
| vo2max | `vo2max` (>0) | UP | never | always |
| rmssd | `ecgSessionRmssd` (>0) | UP | never | always |
| sdnn | `sdnn` (>0) | UP | never | always |
| drift | `cardiacDriftBpmMin` (≠0) | DOWN | `>1.0` | `<0.5` |
| poincareRatio | `poincareRatio` (>0) | FLAT | `<1.0 or >6.0` | `1.5..4.5` |

Rhythm counters (sum over the 28-day window, own thresholds):
```
afib      = sum(afibSuspicionEpisodes)  → GREEN 0,  YELLOW <=2,  RED otherwise
pauses    = sum(ecgPauseCount)          → GREEN 0,  YELLOW <=2,  RED otherwise
premature = sum(ecgPacCount)            → GREEN <=20, YELLOW <=60, RED otherwise
uneven    = sum(ecgIrregularBeats)      → GREEN <=30, YELLOW <=100, RED otherwise
```

### 11.2 Self-diagnosis alert strings (generated from the above)
```
if afib_episodes > 0:
    "{n} AFib-suspicious episode(s) in last 4 weeks — consult a physician if recurring."
if pauses > 2:
    "{n} pauses > 2s in last 4 weeks — uncommon, mention to doctor."
if len(recent_resting_hr) >= 3 and len(baseline_resting_hr) >= 3:
    d = mean(recent_resting_hr) - mean(baseline_resting_hr)
    if d >= 5:
        "Resting HR up {+d} BPM vs baseline — possible fatigue or illness."
if len(recent_hrr) >= 3 and mean(recent_hrr) < 12:
    "HRR chronically below 12 BPM — poor recovery trend."
if total_sessions_in_window < 4:
    "Only {n} session(s) in last 4 weeks — trends not yet reliable."
```

### 11.3 Long-horizon weekly/monthly medians
```
median(values):
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n even: return mean(sorted_values[n/2 - 1], sorted_values[n/2])
    else:      return sorted_values[n // 2]
```
- **Resting HR weekly**: 8-week rolling window, weekly buckets keyed by the Monday of each
  session's week, value = `median(restingHr where >0)` per bucket.
- **HRR60s weekly**: same bucketing, `median(hrr60s where >0)`.
- **VO2max**: **4-week buckets** (not weekly) over a 6-month fetch window, bucket boundary
  = `window_start + floor(weeks_since_start / 4) * 4 weeks`, value = `median(vo2max where >0)`.
- **Scale weight/BMI/body-fat/lean-mass**: 8-week rolling window, weekly buckets (Monday-
  keyed), median per bucket. Applied separately to `weightKg`, `bmi` (filter `>0`),
  `bodyFatPercent` (filter `>0`), `leanMassPercent` (filter `bodyFatPercent > 0`, i.e. only
  when a real BIA reading exists that week).

### 11.4 Average weekly delta (simple trend slope for captions)
```
average_weekly_delta(series) = (series[-1] - series[0]) / (len(series) - 1)
                                # null if fewer than 2 points
```
Algebraically equivalent to `mean(series[i] - series[i-1] for i in 1..n-1)` — just the
endpoints matter, not each individual delta.

---

## 12. Acute:Chronic Workload Ratio (ACWR) — training-load semaphore for the strength side

New addition (2026-08), not ported from the phone — the phone never computed this. Added
as the strength-side counterpart to §11.1's cardio trend card, on the same recent-vs-
baseline semaphore idea, after a research pass turned up ACWR as the one training-load
metric with a direct, low-friction mapping onto data already stored (`sessions.total_tonnage`).
See the research note for the full literature review and the other candidates considered
(session-RPE, bilateral asymmetry, dynapenia) and why they weren't picked for this pass.

```
acute_load   = mean(total_tonnage over the last 7 days, sessions with tonnage > 0)
chronic_load = mean(total_tonnage over the last 28 days, sessions with tonnage > 0)
acwr = acute_load / chronic_load     if chronic_load > 0 else None

semaphore = GRAY   if acute_load == 0 or chronic_load == 0   # not enough data yet
          = GREEN  if 0.8 <= acwr <= 1.3
          = YELLOW if 0.5 <= acwr < 0.8 or 1.3 < acwr <= 1.5
          = RED    if acwr < 0.5 or acwr > 1.5
```

**Window**: acute = last 7 days (inclusive of today), chronic = last 28 days (inclusive of
today) — same two windows already fetched for the cardio card, just a different aggregate
(mean tonnage instead of mean HR/HRV) and a different bucketing (rolling 7/28 from `today`,
not the cardio card's non-overlapping recent-14/baseline-14 split, since ACWR is defined
specifically as acute-window-nested-inside-chronic-window, not two disjoint halves).

**Green band note**: research (see review note) reports 0.8–1.5 as the commonly-cited
"safe" range with >1.5 flagged as elevated risk, but doesn't establish a single accepted
cutoff for a finer-grained good/caution split. Treat 0.8–1.3 (green) / 1.3–1.5 (yellow) as
a deliberately conservative choice for a single-user tool with no coach oversight, not a
literature-derived threshold — reconsider if it proves too twitchy in practice.

**Caveat to preserve in any UI copy**: ACWR predictive validity for injury risk is
contested (see research note's sources) — surface this as an informational trend, not a
medical claim, same posture as the cardio card's existing "not a diagnosis" framing.

---

## 13. Session-RPE (Foster method) — subjective training load

New addition (2026-08), phone-side field added specifically to feed this — see the
research note's session-RPE section for why: it's the standard "internal load" measure
complementary to §12's tonnage-based "external load," and the app previously collected
nothing subjective at all.

**Fields synced as-is** (phone computes and sends both — no server-side recomputation
needed, same "already computed" posture as §2/§3/§4's cardio formulas):

```
sessionRpe: Int | None      # 1-10, user's post-session effort rating; None if skipped
sessionLoad: Float | None   # Foster: sessionRpe * durationMinutes; None if sessionRpe is None
```

**Parsing convention** (see `app/parsing.py`'s `_optional_int`/`_optional_num`): unlike
every other numeric session field, a missing value stays `None`, not 0 — 0 isn't a valid
RPE, so "not answered" must stay distinguishable from an (invalid) reported effort of
zero. `sessions.session_rpe`/`session_load` are nullable columns for the same reason.

**Chart series**: same 12-week window as §1.6's tonnage/e1RM trend and §12's ACWR chart,
plotting `sessionLoad` per session (skipping sessions where it's `None`) — no derived
semaphore for this pass, since there's no published RPE-based threshold as directly
applicable as ACWR's 0.8-1.5 band (session-RPE is normally compared against an athlete's
own history, not an absolute scale). A future pass could compute an ACWR-style
acute:chronic ratio on `sessionLoad` the same way §12 does on tonnage, once enough
history has accumulated to judge whether it adds signal beyond the tonnage-based one.

---

## Server-side data availability gaps to resolve before implementing

1. **7-day resting-HR baseline for VO2max** (§4.2) and **14-day LnRMSSD baseline for
   readiness z-score** (§5.3): currently phone-only state (SharedPreferences), not part of
   the synced payload. To recompute/validate these server-side, query the last 7 (or 14)
   `readiness_events` rows for the relevant field — this should already be sufficient once
   enough readiness events have synced, no new phone-side field needed.
2. **Per-second/per-sample HR series** (used for calorie/TRIMP/cardiac-drift incremental
   computation, §2/§3/§8): NOT currently synced — only the final aggregate values
   (`sessionCalories`, `sessionTrimp`, `cardiacDriftBpmMin`) are. These formulas are
   included for reference/validation only; you cannot recompute them server-side from the
   session record alone unless raw HR series sync is added later (out of scope for now).
3. **Raw ECG samples**: never synced (deleted from the phone after analysis, per
   `docs/CONVENTIONS.md`'s "ECG analysis: keep the raw file on failure" — but even on
   success, raw samples aren't sent to the server, only the derived metrics). §6 is
   reference-only for the same reason as #2.

## Recommended implementation priority

1. **§11 (trend aggregation + self-diagnosis alerts)** — this is the actual "weekly
   analysis" deliverable; everything else is either already computed (just needs storing/
   querying, see the "already computed" section at the top) or not reconstructable
   server-side yet (the gaps above).
2. **§1 (tonnage/1RM/PR analysis)** — second priority, useful for a training-progress view
   independent of the cardio side.
3. Skip §9 (recovery state) unless a specific use case comes up — it's a live in-workout
   UI concept, not a retrospective metric.
4. §2/§3/§6/§8 (calorie/TRIMP/ECG/drift formulas) — reference only, already computed
   phone-side and synced as final values; no server-side recomputation needed unless you
   later decide to sync raw per-sample data too.
