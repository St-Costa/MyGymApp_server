"""Shared fixtures: isolate every test behind its own temp DATA_ROOT/secret so
tests never touch a real deployment's data or share state with each other."""

import gzip
import hashlib
import importlib
import random
import struct
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

# Sentinel: "caller didn't pass this arg" vs. an explicit None (which for
# sleepQuality means "send JSON null"). Only post_readiness needs it so far.
_UNSET = object()

SESSION_MD = """---
id: 3c4d5e6f
date: 2025-04-15
routineId: rt-b2c3d4e5
routineName: Push Day
startedAt: 2025-04-15T09:15:00
completedAt: 2025-04-15T10:05:42
totalTonnage: 12450.0
tonnageByBodypart:
  chest: 8200.0
  shoulders: 4250.0
sessionCalories: 412.3
sessionTrimp: 87.5
vo2max: 48.2
ecgBeats: 5240
ecgDurationSec: 2950
ecgSessionRmssd: 38.2
ecgPacCount: 2
ecgPauseCount: 0
ecgIrregularBeats: 4
afibSuspicionEpisodes: 0
sdnn: 55.0
pnn50: 12.4
poincareSd1: 27.1
poincareSd2: 85.6
poincareRatio: 0.317
cardiacDriftBpmMin: 0.42
restingHr: 58
hrr60s: 28
exercises:
  - exerciseId: ex-a1b2c3d4
    exerciseName: Bench Press
    type: FORZA
    bodypart: chest
    excludeFromTonnage: true
    isDaily: true
    sets:
      - reps: 8
        weight: 80.0
      - reps: 6
        weight: 85.0
---

Optional free-text session notes go here.
"""

READINESS_MD = """---
id: a1b2c3d4
measuredAt: "2026-08-05T07:04:10.123"
readiness: "GOOD"
lnRmssd: 4.30
restingHr: 65
vo2max: 45.2
sleepQuality: 4
recommendation: "HRV above baseline. Good day to push intensity."
---
"""


def recent_readiness_md(*, resting_hr: int = 65) -> bytes:
    """A readiness fixture with `measuredAt` pinned to "today" at call time,
    not READINESS_MD's fixed 2026-08-05 — needed by any test relying on
    app.analysis.resting_hr_7day_average's trailing window actually
    containing the reading (a fixed past date would silently fall outside
    that window as real wall-clock time moves forward past it; this broke
    once already — see the git history of this fixture)."""
    measured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000")
    return (
        f'---\nid: a1b2c3d4\nmeasuredAt: "{measured_at}"\nreadiness: "GOOD"\n'
        f"lnRmssd: 4.30\nrestingHr: {resting_hr}\nvo2max: 45.2\nsleepQuality: 4\n"
        'recommendation: "HRV above baseline. Good day to push intensity."\n---\n'
    ).encode()


SCALE_WEIGHIN_MD = """---
id: "2026-08-05"
date: "2026-08-05"
recordedAt: "2026-08-05T07:12:34"
weightKg: 78.4
bmi: 24.1
bodyFatPercent: 16.8
leanMassPercent: 81.2
---
"""


def recent_scale_weighin_md(*, weight_kg: float = 78.4) -> bytes:
    """A scale-weighin fixture with date/recordedAt pinned to "today" at
    call time, not SCALE_WEIGHIN_MD's fixed 2026-08-05 — needed by any test
    relying on app.analysis.nearest_scale_weight_kg's _NEAREST_WEIGHT_MAX_DAYS
    window actually containing the reading. Same reasoning as
    recent_readiness_md above."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return (
        f'---\nid: "{today}"\ndate: "{today}"\nrecordedAt: "{today}T07:12:34"\n'
        f"weightKg: {weight_kg}\nbmi: 24.1\nbodyFatPercent: 16.8\nleanMassPercent: 81.2\n---\n"
    ).encode()


EXERCISE_MD = """---
id: ex-a1b2c3d4
name: Bench Press
type: FORZA
bodypart: chest
link: https://example.com/bench.png
defaultRepRangeMin: 8
defaultRepRangeMax: 12
isBodyweight: false
created: 2025-03-15T10:22:14
updated: 2025-04-18T09:00:00
---

Form cues: retract scapula, controlled eccentric.
"""

ROUTINE_MD = """---
id: rt-b2c3d4e5
name: Push Day
day: monday
enabled: true
exercises:
  - exerciseId: ex-a1b2c3d4
    sets: 4
    repRangeMin: 6
    repRangeMax: 10
    timePerSetSeconds: 60
    supersetWithNext: false
    isWarmup: true
  - exerciseId: ex-b2c3d4e5
    sets: 3
    repRangeMin: 8
    repRangeMax: 12
---

Routine notes.
"""

SHARED_SECRET = "test-secret-for-unit-tests"


def content_hash_of(raw_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def build_ecg_bytes(
    *, sample_rate: int = 130, start_timestamp_ns: int = 1_700_000_000_000_000_000, samples=None
) -> bytes:
    """Builds a well-formed .ecg binary payload per ECG_SPEC.md's "Binary
    format": magic + sample_rate(i32be) + start_timestamp_ns(i64be) + samples
    (i16be each). Defaults to 650 samples (5s at 130Hz) so it clears the
    phone-side "sampleRate * 5" minimum the spec mentions mirroring."""
    if samples is None:
        samples = [1000] * (sample_rate * 5)
    header = b"MYGMECG1" + struct.pack(">i", sample_rate) + struct.pack(">q", start_timestamp_ns)
    body = b"".join(struct.pack(">h", s) for s in samples)
    return header + body


def synthetic_ecg_samples(
    sample_rate: int = 130, duration_s: int = 20, bpm: float = 75, *, seed: int = 1
) -> list[int]:
    """Low-amplitude noise with a sharp narrow spike every beat interval,
    simulating a QRS complex against a noisy baseline — enough for
    app.ecg_analysis.detect_r_peaks to find real peaks, unlike
    build_ecg_bytes' default flat/constant samples (which have no QRS shape
    at all and correctly produce a "no result" analysis). Shared between
    test_ecg_analysis.py (direct sample-list tests) and
    test_ecg_endpoint.py/test_analyze_ecg.py (which need real .ecg bytes)."""
    rng = random.Random(seed)
    n = sample_rate * duration_s
    samples = [rng.randint(-5, 5) for _ in range(n)]
    beat_interval = round(60.0 / bpm * sample_rate)
    for p in range(beat_interval, n - beat_interval, beat_interval):
        for offset in range(-3, 4):
            if 0 <= p + offset < n:
                samples[p + offset] += 800 - abs(offset) * 100
    return samples


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Reloads app.config/app.storage/app.ingestion/app.routes/app.main so
    each test gets a fresh DATA_ROOT and DB file, rather than sharing the
    module-level singletons across tests."""
    monkeypatch.setenv("SYNC_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))

    import analyze_ecg
    import app.config
    import app.ecg_worker
    import app.git_backup
    import app.gui
    import app.ingestion
    import app.main
    import app.repo
    import app.routes
    import app.security
    import app.storage

    importlib.reload(app.config)
    importlib.reload(app.security)
    importlib.reload(app.storage)
    # git_backup/repo bind `from app.config import RAW_DIR` at import time —
    # reload them after app.config so each test's tmp_path DATA_ROOT takes.
    importlib.reload(app.git_backup)
    importlib.reload(app.repo)
    import app.repo_worker

    importlib.reload(app.repo_worker)
    # analyze_ecg binds RAW_DIR/DB_PATH at import and ecg_worker binds
    # analyze_ecg.analyze_one at import — reload in this order so the worker
    # thread the lifespan starts sees this test's tmp paths, not the last
    # test's.
    importlib.reload(analyze_ecg)
    importlib.reload(app.ecg_worker)
    importlib.reload(app.ingestion)
    importlib.reload(app.routes)
    importlib.reload(app.gui)
    importlib.reload(app.main)

    with TestClient(app.main.app) as client:
        yield client


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {SHARED_SECRET}"}


def settle_backup() -> None:
    """docs/backup/OPTIMIZATION.md: the git commit is debounced and the
    repo-file SQL-view parse runs on a worker thread. A test that inspects
    `git log` or the exercises/routines tables right after a POST must first
    let both catch up. Call this between "POST returned 2xx" and any
    assertion on committed state or parsed rows."""
    import app.git_backup
    import app.repo_worker

    app.repo_worker.drain(timeout=10.0)
    app.git_backup.flush_pending_commit(timeout=10.0)


def settle_ecg() -> None:
    """POST /v1/ecg returns after the header commit; the waveform analysis
    drains on a worker thread. A test that inspects `ecg_analysis` or
    `analyzed_at` right after a POST must call this first — same idea as
    settle_backup. Not needed for pure status-code tests or for uploads
    whose analysis was monkeypatched to boom (the worker booms too and
    writes nothing either way)."""
    import app.ecg_worker

    app.ecg_worker.drain(timeout=30.0)


def _post_record(
    client,
    endpoint: str,
    id_field: str,
    record_id: str,
    md_bytes: bytes,
    *,
    rel_path: str,
    content_hash: str | None,
    headers: dict | None,
    extra_envelope: dict | None = None,
):
    import json

    envelope = {
        id_field: record_id,
        "relPath": rel_path,
        "contentHash": content_hash or content_hash_of(md_bytes),
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
        **(extra_envelope or {}),
    }
    return client.post(
        endpoint,
        data={"envelope": json.dumps(envelope)},
        files={"file": ("record.md", md_bytes, "text/markdown")},
        headers=headers if headers is not None else auth_headers(),
    )


def post_session(
    client,
    md_bytes: bytes = SESSION_MD.encode(),
    *,
    session_id="3c4d5e6f",
    rel_path=None,
    content_hash=None,
    headers=None,
):
    return _post_record(
        client,
        "/v1/sessions",
        "sessionId",
        session_id,
        md_bytes,
        rel_path=rel_path or f"2025/04/2025-04-15_rt-b2c3d4e5_{session_id}.md",
        content_hash=content_hash,
        headers=headers,
    )


def post_readiness(
    client,
    md_bytes: bytes = READINESS_MD.encode(),
    *,
    event_id="a1b2c3d4",
    rel_path=None,
    content_hash=None,
    headers=None,
    steps_avg_per_day=None,
    steps_days_spanned=None,
    omit_steps_keys=False,
    sleep_quality=_UNSET,
    omit_sleep_quality_key=False,
):
    # SYNC.md "Daily step average": the real phone always sends both keys,
    # defaulting here to explicit JSON null (not omission) to match that
    # contract by default. omit_steps_keys exists only to test the
    # leniency fallback in routes.py for a sender that doesn't.
    extra_envelope = (
        {}
        if omit_steps_keys
        else {"stepsAvgPerDay": steps_avg_per_day, "stepsDaysSpanned": steps_days_spanned}
    )
    # READINESS_SPEC.md: the real phone always sends sleepQuality (int 1..5
    # or explicit null). Default matches READINESS_MD's frontmatter (4), the
    # "user rated it" case; pass sleep_quality=None for explicit null,
    # sleep_quality=<bad> for the 400 test, or omit_sleep_quality_key=True
    # for the missing-key leniency case.
    if not omit_sleep_quality_key:
        extra_envelope["sleepQuality"] = 4 if sleep_quality is _UNSET else sleep_quality
    return _post_record(
        client,
        "/v1/readiness",
        "eventId",
        event_id,
        md_bytes,
        rel_path=rel_path or f"readiness/{event_id}.md",
        content_hash=content_hash,
        headers=headers,
        extra_envelope=extra_envelope,
    )


def post_scale_weighin(
    client,
    md_bytes: bytes = SCALE_WEIGHIN_MD.encode(),
    *,
    weighin_id="2026-08-05",
    rel_path=None,
    content_hash=None,
    headers=None,
):
    return _post_record(
        client,
        "/v1/scale-weighins",
        "weighInId",
        weighin_id,
        md_bytes,
        rel_path=rel_path or f"2026/08/{weighin_id}.md",
        content_hash=content_hash,
        headers=headers,
    )


def post_repo(
    client,
    md_bytes: bytes | None = EXERCISE_MD.encode(),
    *,
    rel_path: str = "exercises/bench-press-ex-a1b2c3d4.md",
    op: str = "upsert",
    content_hash: str | None = None,
    headers: dict | None = None,
    omit_field: str | None = None,
):
    """POST /v1/repo (docs/backup-server-brief.md §2). For op='delete' pass
    md_bytes=None — no `file` part is sent. `omit_field` drops one required
    envelope key, for the 422 tests."""
    import json

    envelope = {
        "relPath": rel_path,
        "op": op,
        "contentHash": content_hash
        or (content_hash_of(md_bytes) if md_bytes is not None else "sha256:" + "0" * 64),
        "appVersion": "1.0-test",
        "clientSentAt": "2026-09-04T20:11:03Z",
    }
    if omit_field is not None:
        envelope.pop(omit_field, None)

    files = None
    if md_bytes is not None and op == "upsert":
        files = {"file": ("record.md", md_bytes, "text/markdown")}

    return client.post(
        "/v1/repo",
        data={"envelope": json.dumps(envelope)},
        files=files,
        headers=headers if headers is not None else auth_headers(),
    )


def post_ecg(
    client,
    ecg_bytes: bytes | None = None,
    *,
    session_id="3c4d5e6f",
    rel_path=None,
    content_hash=None,
    headers=None,
    compressed_override: bytes | None = None,
):
    """Unlike the other three record types, the wire payload is gzip-
    compressed and contentHash is computed over the *compressed* bytes (see
    ECG_SPEC.md point 2) — so this doesn't reuse `_post_record`, which hashes
    the part's bytes directly. `compressed_override` lets a test send bytes
    that don't gunzip cleanly, or whose hash deliberately doesn't match, by
    bypassing the "compress ecg_bytes for me" convenience."""
    import json

    if compressed_override is not None:
        compressed = compressed_override
    else:
        compressed = gzip.compress(ecg_bytes if ecg_bytes is not None else build_ecg_bytes())

    envelope = {
        "sessionId": session_id,
        "relPath": rel_path or f"ecg/{session_id}.ecg",
        "contentHash": content_hash or content_hash_of(compressed),
        "encoding": "gzip",
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
    }
    return client.post(
        "/v1/ecg",
        data={"envelope": json.dumps(envelope)},
        files={"file": ("record.ecg.gz", compressed, "application/gzip")},
        headers=headers if headers is not None else auth_headers(),
    )


def post_sessions_bulk(
    client,
    sessions: list[dict],
    files: list[bytes],
    *,
    headers=None,
):
    """Bulk POST N sessions in one multipart request. `sessions` is a list of
    envelope dicts (each with sessionId, relPath, contentHash, appVersion,
    clientSentAt). `files` is a list of raw markdown bytes matching the
    envelopes in order. Returns the JSON response."""
    import json

    # Build multipart form
    envelope_json = json.dumps(sessions)
    form_data = {"envelope": envelope_json}
    form_files = []
    for i, file_bytes in enumerate(files):
        form_files.append((f"file_{i}", (f"session_{i}.md", file_bytes, "text/markdown")))

    return client.post(
        "/v1/sessions/bulk",
        data=form_data,
        files=form_files,
        headers=headers if headers is not None else auth_headers(),
    )


def post_ecg_bulk(
    client,
    ecgs: list[dict],
    files: list[bytes],
    *,
    headers=None,
):
    """Bulk POST N ECG recordings in one multipart request. `ecgs` is a list
    of envelope dicts (each with sessionId, relPath, contentHash, encoding,
    appVersion, clientSentAt). `files` is a list of gzipped ECG bytes matching
    the envelopes in order. Returns the JSON response."""
    import json

    envelope_json = json.dumps(ecgs)
    form_data = {"envelope": envelope_json}
    form_files = []
    for i, file_bytes in enumerate(files):
        form_files.append((f"file_{i}", (f"ecg_{i}.ecg.gz", file_bytes, "application/gzip")))

    return client.post(
        "/v1/ecg/bulk",
        data=form_data,
        files=form_files,
        headers=headers if headers is not None else auth_headers(),
    )
