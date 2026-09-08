# Cardio blocks — `exercise_sets` schema addendum for `ExerciseType.CARDIO`

Status: **implemented server-side** (`exercise_sets.started_at/ended_at/avg_hr/max_hr`,
plus the parser branch — see `app/storage.py` and `app/parsing.py`). Phone-side transport requires **zero** change
to `POST /v1/sessions` or `POST /v1/ecg` — this is purely an addendum to how the existing
`exercise_sets` parser/table handle a session whose `exercises[].type` is now sometimes
`CARDIO` instead of only `FORZA`/`STRETCH`. Read `SPEC.md` (this directory) first — this
document only covers what's new/different.

Source of truth for the phone-side implementation this spec describes: `docs/POLAR.md`
("Cardio blocks" section) and `docs/STORAGE.md` (session YAML shape) and `docs/SYNC.md`
("Cardio blocks — deriving ECG segments without touching the binary format") in the
`MyGymApp` repo (not this one).

## Why this exists

The phone can now mark an explicit "this part of the session is cardio" phase — one or
more continuous blocks per session (e.g. warmup → strength → 20 min run → stretching),
each with its own start/end time and on-device-computed avg/max heart rate. This is a
third `ExerciseType` alongside the existing `FORZA`/`STRETCH`, sent through the **same**
`POST /v1/sessions` envelope as always — no new endpoint, no change to the raw `.md` file
transport, no change to the session-level frontmatter fields already in `sessions`.

What's new is only the *shape* of one exercise's `sets` array. Today the parser assumes
every set is either `{reps, weight}` (FORZA) or `{timeSeconds, done}` (STRETCH). It must
now also accept `{startedAt, endedAt, avgHr, maxHr}` when `exercises[].type == "CARDIO"`.

## What did NOT change

- `POST /v1/sessions` request/response contract — identical.
- `POST /v1/ecg` request/response contract, binary `.ecg` format, `ecg_recordings` table —
  **completely untouched**. The raw ECG stream is still one continuous recording per
  session, exactly as before. See "Deriving cardio-block ECG segments" below for how this
  new data gets *used* — no ingestion change is needed for it.
- `sessions` table — no new columns needed here; cardio blocks live entirely in
  `exercise_sets` (per-set data), same tier as reps/weight/timeSeconds/done.
- Idempotency, content-hash, raw-first storage — all unchanged, since this is still one
  raw `.md` file synced through the existing endpoint.

## Session YAML shape (for the parser)

```yaml
exercises:
  - exerciseId: ex-c9d8e7f6
    exerciseName: Corsa leggera
    type: CARDIO
    bodypart: cardio
    excludeFromTonnage: true
    sets:
      - startedAt: "2026-08-12T10:15:03"
        endedAt: "2026-08-12T10:45:10"
        avgHr: 138
        maxHr: 156
      - startedAt: "2026-08-12T10:50:00"
        endedAt: ""              # blank = block still running or abandoned mid-recording;
                                  # never blank on an exercise the phone marked `completed`
        avgHr: 0
        maxHr: 0
```

`startedAt`/`endedAt` are **absolute ISO `LocalDateTime` strings** (phone-local time, no
timezone suffix — same convention as `session.startedAt`/`completedAt` elsewhere in the
existing frontmatter, i.e. treat as naive local time, not UTC). This is deliberate: it is
what lets the ECG-correlation step below work without any offset math needing to happen on
the phone.

## `exercise_sets` schema addendum

Add four nullable columns to the existing table rather than a separate `cardio_sets`
table — a cardio set is still conceptually one row of "this exercise, this set index,
these fields," same as a strength or stretch set; a parallel table would just mean two
places to join per session instead of one, for no real benefit at this scale.

```sql
ALTER TABLE exercise_sets ADD COLUMN started_at TEXT;   -- ISO local datetime, CARDIO only
ALTER TABLE exercise_sets ADD COLUMN ended_at   TEXT;   -- ISO local datetime, CARDIO only,
                                                          -- may be NULL/'' if abandoned mid-block
ALTER TABLE exercise_sets ADD COLUMN avg_hr     INTEGER; -- CARDIO only
ALTER TABLE exercise_sets ADD COLUMN max_hr     INTEGER; -- CARDIO only
```

`exercise_type` already exists as a free-text column (`'FORZA' | 'STRETCH'`) — simply
widen the accepted values to include `'CARDIO'`; no migration needed for that column
itself, only the four new ones above. For non-CARDIO rows these four columns are `NULL`
(not `0`/`''`) — unlike the phone's own YAML-omission convention (0 for numbers, blank for
strings), here `NULL` is the right choice specifically because `0`/`''` are themselves
valid values for a real (if degenerate) cardio block, so they can't double as "not
applicable." `reps`/`weight` stay `NULL` for CARDIO rows for the same reason, mirroring how
they're presumably already handled for STRETCH rows today.

### Parser changes (`exercise_sets` insert step)

In the per-set loop that builds `exercise_sets` rows (`SPEC.md` step 6 / the shared
parsing function also used by `reparse.py`), branch on `exercise.type` the same way it
presumably already branches for FORZA vs STRETCH field extraction:

```python
if exercise["type"] == "CARDIO":
    row["started_at"] = set_["startedAt"] or None
    row["ended_at"] = set_["endedAt"] or None
    row["avg_hr"] = set_.get("avgHr") or None
    row["max_hr"] = set_.get("maxHr") or None
    # reps, weight stay None
else:
    # existing FORZA/STRETCH extraction, unchanged
```

Bump `PARSER_SCHEMA_VERSION` (the same constant used for `sessions.schema_version`) so
existing synced sessions containing a CARDIO exercise — if any were already accepted with
`schema_version` from before this change landed and silently dropped their cardio sets
(per the existing "unknown fields don't fail parsing, they're just not extracted yet"
tolerance) — get flagged for `reparse.py --stale` once the parser is updated. This is the
same mechanism already used for `isBodyweight`/`sessionRpe` per `SYNC.md`'s "second/third
example, same pattern."

## `excludeFromTonnage` for CARDIO

The phone always sends `excludeFromTonnage: true` for a CARDIO exercise regardless of
section (warmup/daily/normal) — no server-side special-casing needed, the existing
`exclude_from_tonnage` column and any tonnage-summing query that already filters on it
handles this correctly with zero changes, the same way it already excludes warmup/daily
rows.

## Deriving cardio-block ECG segments (analysis-time, not ingestion-time)

This is **not** part of the ingestion/transport contract above — it's a note for whatever
analysis job eventually reads `ecg_recordings` + `exercise_sets` together (see
`ECG_ANALYSIS_HANDOFF.md` for the general ECG analysis this would build on). Once both a
session's `exercise_sets` rows (with CARDIO `started_at`/`ended_at`) and its
`ecg_recordings` row (with `sample_rate`/`start_timestamp_ns`) exist for the same
`session_id`, a cardio block's sample range within the raw waveform is:

```python
start_sample = (parse(started_at) - start_timestamp) * sample_rate
end_sample   = (parse(ended_at)   - start_timestamp) * sample_rate
```

This lets analysis tag/slice the ECG waveform specifically for the cardio phase (as
opposed to warmup/strength segments), without any change to how the `.ecg` file itself was
captured or stored — the phone never segmented the recording; the segmentation is entirely
a server-side, analysis-time computation from two already-synced pieces of data. Absolute
timestamps (rather than the phone pre-computing a sample offset) avoid compounding any
drift between the ECG stream's nominal vs. actual sample rate over a long recording — do
this arithmetic in floating point at analysis time, not once and cached, if `sample_rate`
is ever revised.

`started_at`/`ended_at` are phone-local naive datetimes with no timezone info, same as
`start_timestamp_ns` in the `.ecg` header is `ns since epoch` in the phone's own clock —
both come from the same device's clock, so no timezone conversion is needed for this
correlation to be correct; do not attempt to interpret either as UTC.

## What NOT to build yet

- **No new endpoint.** Cardio blocks ride the existing `POST /v1/sessions` transport.
- **No cardio-specific analysis job.** This spec only covers making the data
  queryable (`exercise_sets` columns); computing anything from it (e.g. "average HR this
  cardio exercise trended down 5% over 4 weeks," ECG-segment tagging above) is future,
  separate work once there's real synced data to look at.
- **No dedicated `cardio_blocks` table.** See "schema addendum" above for why the existing
  `exercise_sets` table is the right home.

## Implementation checklist for `MyGymApp_server`

- [ ] `ALTER TABLE exercise_sets ADD COLUMN started_at/ended_at/avg_hr/max_hr` (or include
      in the `CREATE TABLE` from the start if the schema hasn't shipped yet)
- [ ] Widen `exercise_type` accepted values to include `'CARDIO'` (free-text column,
      no enum/CHECK constraint currently — confirm no such constraint was added since
      `SPEC.md` was written before assuming this is a no-op)
- [ ] Parser: branch on `exercise.type == "CARDIO"` when building `exercise_sets` rows,
      extracting `startedAt`/`endedAt`/`avgHr`/`maxHr` instead of `reps`/`weight`/
      `timeSeconds`/`done`
- [ ] Bump `PARSER_SCHEMA_VERSION`; confirm `reparse.py --stale` picks up any
      already-synced sessions containing a CARDIO exercise once the parser is updated
- [ ] Testing: send a hand-written session `.md` with a CARDIO exercise (see YAML shape
      above) through `POST /v1/sessions`, confirm `exercise_sets` rows land with the four
      new columns populated and `reps`/`weight` NULL
- [ ] Testing: confirm a session with **only** FORZA/STRETCH exercises (no CARDIO) is
      completely unaffected — all four new columns NULL, no regression in existing rows
- [ ] Testing: confirm `excludeFromTonnage: true` on a CARDIO exercise is already handled
      correctly by any existing tonnage-summing query (should require no code change)
- [ ] Document the sample-offset formula above wherever the eventual ECG analysis job's
      design doc lives (`ECG_ANALYSIS_HANDOFF.md` or its successor), so it isn't
      re-derived from scratch when that work starts
