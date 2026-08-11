# ECG analysis handoff: what the phone used to do, and what the server needs to do now

## Context — read this first

Until recently, the phone ran a complete offline ECG analysis after every workout session:
Pan-Tompkins R-peak detection on the raw waveform, RR-interval-derived HRV metrics
(RMSSD, SDNN, pNN50, Poincaré SD1/SD2/ratio), and simple arrhythmia screening (PAC count,
pause count, irregular-beat count, AFib suspicion episodes). The 12 resulting numbers were
written into the session YAML and synced via the existing `POST /v1/sessions` — the server
never saw the raw waveform, only the finished numbers.

Two changes landed in the phone app, in this order:

1. **Raw ECG sync added** (`ECG_SPEC.md`, already implemented both sides — receiver commit
   `cdfb74f`): the phone now also uploads the raw waveform itself via `POST /v1/ecg`,
   gzip-compressed, so the server can run a heavier/more accurate/potentially AI-assisted
   analysis than a phone can — more CPU budget, and comparison against the user's full
   history rather than one isolated session.
2. **Local deep analysis removed** (phone-side only, no server impact until now): the
   phone **no longer runs `EcgAnalyzer.analyze()` at all**. `ActiveRoutineViewModel` no
   longer calls it, and the 12 fields above are no longer computed or written to new
   sessions' YAML — they stay at their zero defaults going forward. The phone now only
   computes and shows what doesn't need the ECG waveform: resting HR, VO2max (from live
   HR/readiness tracking), plus TRIMP/kcal (Banister/Keytel, continuous) and cardiac
   drift/HRR60s (cheap HR-series math, not waveform analysis) — all still computed and
   synced exactly as before.

**Net effect**: raw ECG waveforms are now landing in `ecg_recordings` (via `POST /v1/ecg`),
but nothing computes the 12 derived metrics from them anymore, on either side. That's the
gap this document exists to close — a complete, from-source specification of exactly what
the phone used to compute, so the server can reimplement it (or improve on it) without
guessing at the phone's intent.

This is the concrete instance of the pattern `SYNC.md`'s "core design problem" section
anticipated for the whole sync system: raw data lands durably regardless of whether the
parser/analysis logic understands it yet, and the interpretation layer can be built (or
rebuilt, or upgraded) independently, on your own schedule, without touching the phone.

## What existed and was real (not aspirational)

This section is the actual, shipped, working implementation — verified against the
Kotlin source at the commit where it was last active
(`app/src/main/java/com/mygymapp/data/polar/EcgAnalyzer.kt`, still present in the repo,
just unreferenced/uncalled now). This ran in production for real workout sessions; it is
not a design sketch.

### Pipeline overview

```
raw .ecg file (130Hz Int16 µV samples, MYGMECG1 header — see ECG_SPEC.md/STORAGE.md)
  → Pan-Tompkins-style R-peak detection
  → RR intervals (filtered to 300–2500ms as physiologically plausible)
  → HRV metrics (RMSSD, SDNN, pNN50, Poincaré SD1/SD2/ratio)
  → Arrhythmia markers (PAC, pause, irregular beat count)
  → AFib suspicion screening (sliding-window CV + autocorrelation heuristic)
```

Minimum input requirements before analysis proceeds at all (below these, the phone
returned "no result" rather than zeros — same convention recommended server-side):
- File ≥ 32 bytes (header + at least some samples).
- ≥ `sampleRate * 5` samples (5 seconds minimum).
- ≥ 2 detected peaks.
- ≥ 5 valid RR intervals (after the 300–2500ms filter).

### 1. R-peak detection — simplified Pan-Tompkins

Sample rate: 130 Hz (Polar H10 fixed).

```
Step 1 — crude bandpass via cascaded moving averages:
  lp        = moving_average(raw_samples, window = max(2, sample_rate / 15))
  lp_long   = moving_average(lp, window = sample_rate / 5)
  hp        = lp - lp_long                          # elementwise

Step 2 — 5-point derivative:
  deriv[i] = (-hp[i-2] - 2*hp[i-1] + 2*hp[i+1] + hp[i+2]) / 8
  (0 at the boundaries, i < 2 or i >= n-2)

Step 3 — square:
  squared[i] = deriv[i]^2

Step 4 — moving window integration, 150ms window:
  integrated = moving_average(squared, window = round(0.150 * sample_rate))

Step 5 — adaptive threshold peak detection:
  refractory = round(0.25 * sample_rate)   # 250ms minimum between peaks
  local_max  = trailing_max(integrated, window = sample_rate * 5)   # 5s trailing max, O(n) via monotonic deque
  threshold[i] = local_max[i] * 0.5

  for i in 1..n-2:
    if integrated[i] > threshold[i]
       and integrated[i] > integrated[i-1]
       and integrated[i] >= integrated[i+1]
       and (i - last_peak) > refractory:
         record peak at i
         last_peak = i
```

**Why a trailing max, not a global max — this is the single most important detail to
carry over.** An earlier version of this code used `threshold = global_max(integrated) *
0.3` over the *entire* recording. A gym session almost always contains at least one motion
artifact (strap shift, a heavy rep, adjusting the strap) whose integrated value is an order
of magnitude above any real QRS complex. Once that one artifact set the global max, the
threshold was pinned far above any real beat for the rest of the (often multi-hour)
recording — this silently zeroed out beat detection on nearly every real session for
months before being caught (confirmed via `p99.99`-vs-global-max ratio analysis on pulled
`.ecg` files: ~18x on a failing recording vs ~2.7x on a working one). The fix — a 5-second
**trailing** max instead of a whole-file max — means one artifact only poisons detection
for the ~5 seconds around itself, not the whole session. **Do not regress to a global-max
threshold when reimplementing this server-side.**

### 2. RR intervals and basic HRV

```
rr_intervals = [ (peaks[i+1] - peaks[i]) / sample_rate * 1000  for consecutive peak pairs ]
               filtered to 300 <= rr <= 2500  (ms; discards physiologically implausible intervals)

avg_hr        = 60000.0 / mean(rr_intervals)
session_rmssd = sqrt(mean((rr[i+1] - rr[i])^2  for consecutive rr pairs))
mean_rr       = mean(rr_intervals)
sdnn          = sqrt(mean((rr - mean_rr)^2  for each rr))
nn50          = count(pairs where |rr[i+1] - rr[i]| > 50ms)
pnn50         = nn50 / num_pairs * 100
```

### 3. Poincaré plot metrics

```
msd  = mean((rr[i+1] - rr[i])^2  for consecutive rr pairs)     # same "msd" as RMSSD^2
sd1  = sqrt(0.5) * sqrt(msd)
sd2  = sqrt(2 * sdnn^2 - 0.5 * msd)        # clamp to 0.0 if the value under sqrt is negative
ratio = sd2 / sd1   if sd1 > 0   else 0.0
```

### 4. Arrhythmia markers — single pass, sliding local median (window 10)

```
window_size = 10
uncounted_consecutive = false   # tracks a pending "irregular" candidate across one iteration

for i in rr_intervals.indices:
    rr = rr_intervals[i]

    if rr > 2000:
        pause_count += 1
        uncounted_consecutive = false
        continue

    # local median over a centered window of up to 10 RR intervals
    start = max(0, i - 5)
    end   = min(len(rr_intervals), start + 10)
    if (end - start) < 5:
        uncounted_consecutive = false
        continue
    local_median = median(rr_intervals[start:end])

    if rr < local_median * 0.85:
        next_rr = rr_intervals[i+1]  if exists  else None
        if next_rr is not None and next_rr > local_median * 1.10:
            pac_count += 1
        # else: short RR without a compensatory pause — NOT counted as anything
        uncounted_consecutive = false

    elif abs(rr - local_median) > local_median * 0.20 and rr > local_median:
        if uncounted_consecutive:
            irregular_beats += 2      # confirms BOTH the previous and current beat
            uncounted_consecutive = false
        else:
            uncounted_consecutive = true   # wait for a second consecutive deviation

    else:
        uncounted_consecutive = false
```

Definitions, in plain terms:
- **PAC** (premature atrial contraction, screening use of the term — not a clinical
  diagnosis): a short RR interval (<85% of the local median) immediately followed by a
  compensatory long one (>110% of local median).
- **Pause**: any RR interval over 2000ms.
- **Irregular beat**: an RR interval more than 20% away from the local median **and above
  it** (i.e. only counts intervals *longer* than expected as "irregular" in this
  definition — a deliberate asymmetry, not a bug, carried over as-is). Requires **two
  consecutive** such deviations to count, specifically to filter out normal respiratory
  sinus arrhythmia and single-sample artifacts — when confirmed, both the pending and
  current beat are counted (`+= 2`), not just the second one.

### 5. AFib suspicion screening — sliding-window heuristic

**This is explicitly a screening heuristic, not a diagnostic tool — the phone UI always
showed a disclaimer alongside this ("non diagnostico, consulta un medico se persiste/
ricorrente") and any server-side surfacing of this value should keep that framing.**

```
window_len = 30   # RR intervals per window
step       = 15   # 50% overlap between consecutive windows

in_episode = false
i = 0
while i + window_len <= len(rr_intervals):
    window = rr_intervals[i : i + window_len]
    w_mean = mean(window)
    w_sd   = stddev(window)
    cv     = w_sd / w_mean   if w_mean > 0   else 0.0

    # lag-1 autocorrelation: near 0 for AFib-like randomness, positive for normal sinus rhythm
    covariance = mean((a - w_mean) * (b - w_mean)  for consecutive pairs (a,b) in window)
    autocorr   = covariance / w_sd^2   if w_sd > 0   else 0.0

    irregular_window = (cv > 0.12) and (abs(autocorr) < 0.20)

    if irregular_window and not in_episode:
        afib_suspicion_episodes += 1     # count only the RISING EDGE into an irregular run
        in_episode = true
    elif not irregular_window:
        in_episode = false

    i += step
```

### Binary format reminder

Already documented in `ECG_SPEC.md` and `STORAGE.md` — repeated here for convenience
since it's the direct input to §1 above:

```
Offset 0,  8 bytes : magic "MYGMECG1" (ASCII)
Offset 8,  4 bytes : sample rate, Int32 big-endian (always 130 in practice — Polar H10)
Offset 12, 8 bytes : start timestamp, Int64 big-endian, ns since epoch (unused by the old
                     phone-side reader; available if useful server-side)
Offset 20, N×2 bytes: sample stream, Int16 big-endian, µV
```

This is the **decompressed** format — `POST /v1/ecg`'s payload is gzip-compressed in
transit; `ecg_recordings.raw_path` points at the already-decompressed file on disk
(per `ECG_SPEC.md` receiver logic step 8).

## What was designed but never actually built — do not assume this exists

The original research/planning document (`docs/polar/implementation-guide.md` in the
`MyGymApp` repo, §7.8 "Passive Arrhythmia Screening") sketched a richer feature set than
what was ultimately shipped. These were **never implemented** — there is no code for them
anywhere in the phone app, past or present:

- **PVC (premature ventricular contraction) detection** via QRS width/morphology
  analysis. The design doc describes measuring QRS duration (normal 80–120ms, wide
  >120ms suggests PVC) and template-matching morphology against a "normal beat"
  reference. `EcgAnalyzer.kt` has no QRS-width measurement of any kind — it only ever
  detects peak *timing*, not QRS complex shape or duration. If PVC detection is wanted,
  it needs to be designed and built from scratch, not "ported."
  - **New algorithmic ground, not a port**: `panTompkinsDetect()`'s `integrated` array
    from §1 gives you peak *positions*; QRS width would need to be measured on the
    filtered waveform around each peak (e.g. thresholded crossing width), which the
    phone-side pipeline never computed or stored.
- **Respiratory sinus arrhythmia (RSA) via FFT of the RR series** — mentioned in the
  design doc (§7.6-adjacent, "FFT of RR interval series") as a way to extract breathing
  rate. Never built; not part of `EcgAnalyzer.kt`'s output.
- **DFA alpha1 (detrended fluctuation analysis)** for real-time fatigue estimation
  during a set — mentioned in the design doc as a "high complexity" idea. Never built.
- **Personal RMSSD-recovery baselines per exercise type** (e.g. "squat recovery RMSSD
  differs from bicep curl RMSSD") — mentioned as a future adaptive-threshold idea in the
  design doc. Never built; the phone's actual HRR/recovery logic (§7 of `ANALYSIS_SPEC.md`)
  uses a single fixed threshold, not per-exercise personalization.

If any of these are wanted, they're new server-side R&D, not a phone-to-server port — call
that out explicitly rather than treating this document as covering them, since none of the
formulas above touch QRS morphology, frequency-domain analysis, or per-exercise
personalization.

## What this unlocks, now that raw waveform lands server-side

This is the actual motivation for the whole raw-ECG-sync change (see `SYNC.md`'s "Fourth
record type" section in the `MyGymApp` repo for the original reasoning) — now that you have
the primary source data:

- Reimplement §1–§5 above with more compute budget than a phone has, or swap in a better
  QRS detector / HRV toolkit than the simplified from-scratch Pan-Tompkins the phone used.
- Longitudinal analysis: compare each session's beat morphology/HRV against the *user's
  full history* rather than one isolated 60-second local-median window — something a phone
  computing one session at a time structurally cannot do.
- Optionally build the never-shipped features above (PVC/RSA/DFA) for real this time,
  informed by however much historical raw waveform data accumulates.
- Combine ECG-derived metrics with session-RPE/tonnage/ACWR (already flowing through the
  other sync pipelines, see `ANALYSIS_SPEC.md` §12–13) for a more complete picture than
  either signal alone — e.g. correlating cardiac drift/irregular-beat trends with training
  load spikes.

## Suggested implementation approach

1. **Start with a straight port** of §1–§5 exactly as written, against `ecg_recordings`
   rows where `analyzed_at IS NULL` (the queue `ECG_SPEC.md` already sets up for this).
   Verify against a few known sessions before improving anything — this gives you a
   correctness baseline and confirms the raw waveform ingest pipeline is solid before
   layering new analysis logic on top.
2. Store results in a new table (not designed here — `ECG_SPEC.md` deliberately left this
   out of the transport spec; suggested shape mirrors the 12 fields above plus
   `ecg_recordings.session_id` as the join key, with its own `schema_version` for the
   `reparse.py` re-run discipline `ANALYSIS_SPEC.md` and `SYNC.md` already establish).
3. Only after the port is verified working: decide whether to improve the detector, add
   the never-shipped PVC/RSA/DFA features, or layer in cross-session/longitudinal analysis
   — each of those is new work, not part of reproducing what already existed.
