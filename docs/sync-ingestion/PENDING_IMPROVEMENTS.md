# Pending server-side improvements — blocked on data, not code

Living doc, not a historical record like `SPEC.md`/ADRs — update or delete entries here as
they're resolved. Each entry below is a metric/calculation identified as improvable
(see the audit that produced this list, referenced in commit history around the %HRR/
calorie-TRIMP/VO2max recompute work) but currently blocked because the *server* has
nowhere to read the needed input from — not because the formula/join isn't designed yet.

For each entry: what's blocked, what unblocks it, whose responsibility that is, and a
copy-pasteable check to run periodically (there is no automated trigger — nothing polls
this repo or the DB on a schedule; re-running these checks is a manual, occasional task,
e.g. "next time you're touching this area of the code" or "ask your AI agent to check").

---

## 1. Cardiac drift / aerobic decoupling from real CARDIO blocks

**Blocked on**: real synced sessions containing `exercise_sets` rows with
`exercise_type='CARDIO'` (and their `started_at`/`ended_at`/`avg_hr`/`max_hr` populated).

**Whose responsibility**: none — **not an app change**. The schema, parser, and spec
(`CARDIO_BLOCKS_SPEC.md`) already support this end-to-end; it's purely a matter of the
user logging a workout with a CARDIO-typed block through the existing app feature. Once
that happens, the ECG-segment-join approach documented in `CARDIO_BLOCKS_SPEC.md` and
`ECG_ADVANCED_ANALYSIS.md` §5 ("Cardiac drift / aerobic decoupling") is ready to implement
against real data.

**Check** (run from repo root):
```bash
python3 -c "
import sqlite3
from app.config import DB_PATH
conn = sqlite3.connect(DB_PATH)
n = conn.execute(\"SELECT COUNT(*) FROM exercise_sets WHERE exercise_type='CARDIO'\").fetchone()[0]
print('CARDIO exercise_sets rows:', n)
"
```
**Ready when**: `n > 0` for at least a few sessions (one session isn't enough to validate
the join/formula against — a handful gives room to sanity-check across different session
lengths/intensities). As of 2026-08-12: **0**.

---

## 2. BIA / body composition recompute with fresher age — LOW PRIORITY, marginal gain

**Not a real accuracy win — don't over-prioritize this even once unblocked.** Unlike the
other entries here (and unlike the %HRR/calorie-TRIMP/VO2max work already done, where the
server has a genuinely better *input* — real ECG beat-by-beat HR instead of an optical
sensor), a server-side BIA recompute would use the *exact same* Deurenberg-family formula
the phone already uses, with the *same* impedance/height inputs, differing only in using
a dynamically-updated age (`ASSUMED_BIRTH_YEAR`, ticks forward each year automatically)
instead of whatever age was baked into the phone's calculation at measurement time. That's
a rounding-error-scale difference (at most ~1 year of age drift between the two), not a
different-quality measurement. Listed here for audit completeness, not because it's worth
chasing — implement only if the app-side field addition below happens for some *other*
reason and this becomes a near-zero-cost add-on.

**Blocked on**: `impedance_ohm` and `height_cm` being synced from the phone at all —
`scale_weighins` currently only receives the phone's already-computed `bodyFatPercent`/
`leanMassPercent`/`bmi`, never the raw inputs the Deurenberg-family regression needs.

**Whose responsibility**: **app change required**. `SCALE_SPEC.md` would need a new
field (or two) added to the scale-weighin payload, the phone-side BIA scale integration
would need to expose raw impedance/height if it doesn't already, and the server's
`scale_weighins` schema/parser would need the new columns. This is real app-repo work,
not something that unlocks itself through normal use — flag it to whoever maintains the
Android app if this is worth prioritizing.

**Check**: read `docs/sync-ingestion/SCALE_SPEC.md`'s frontmatter field list — if
`impedanceOhm`/`heightCm` (or similarly named fields) appear there, the app has been
updated and this is unblocked. As of 2026-08-12: not present.

---

## 3. `is_bodyweight` — dependent metrics once populated

**Blocked on**: `exercise_sets.is_bodyweight` actually being set to 1 on any real synced
set — the column and the server-side logic that reads it (`_is_untouched_or_empty` in
`app/analysis.py`) are already correct and in place; the phone-side feature that stamps
it is described as "just introduced" in `ANALYSIS_SPEC.md`'s own opening note.

**Whose responsibility**: none — **not an app change**, same as #1: this is a "wait for
real usage to catch up to an already-shipped app feature" situation, not a code gap.

**Check**:
```bash
python3 -c "
import sqlite3
from app.config import DB_PATH
conn = sqlite3.connect(DB_PATH)
n = conn.execute('SELECT COUNT(*) FROM exercise_sets WHERE is_bodyweight=1').fetchone()[0]
print('is_bodyweight=1 rows:', n)
"
```
**Ready when**: `n > 0`. As of 2026-08-12: **0**. (Low priority — the consuming logic
already handles this correctly; this entry exists mainly so a sudden appearance of
bodyweight-flagged data doesn't go unnoticed as "how long has this been available".)

---

## 4. Baseline HRV z-score personalization

**Blocked on**: enough `readiness_events` history to compute a meaningful personal
baseline — `ANALYSIS_SPEC.md` §5.3 needs a 14-day trailing window with a reasonable
number of readings in it; a prior attempt at server-side re-derivation was tried and
explicitly abandoned (see the comment in `app/analysis.py` near `readiness_daily_trend`)
because there wasn't enough synced history yet.

**Whose responsibility**: none — **not an app change**, purely a matter of the user
continuing to log morning readiness measurements through the existing feature.

**Check**:
```bash
python3 -c "
import sqlite3
from app.config import DB_PATH
conn = sqlite3.connect(DB_PATH)
n = conn.execute('SELECT COUNT(*) FROM readiness_events WHERE resting_hr IS NOT NULL AND resting_hr > 0').fetchone()[0]
print('readiness_events with a real resting_hr reading:', n)
"
```
**Ready to revisit when**: comfortably more than 14 (a handful of weeks of daily
readings, not just enough to clear the minimum once) — as of 2026-08-12: **6**.

---

## How to use this doc

- When touching any of the metrics above, or when the user asks about further server-side
  accuracy improvements, run the relevant check(s) above first rather than assuming "still
  blocked" — the whole point of this doc is that these situations resolve themselves
  silently through normal app usage, with no notification.
- When a check's condition is met, treat it as unblocked: implement per the referenced
  spec section, verify against real data (this project's established pattern — see git
  log for the %HRR/calorie-TRIMP/VO2max work), then remove or check off that entry here.
- If a new "blocked on data" situation is identified in a future audit, add it here in the
  same format rather than letting it live only in chat history.
