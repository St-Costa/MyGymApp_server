"""Shared fixtures: isolate every test behind its own temp DATA_ROOT/secret so
tests never touch a real deployment's data or share state with each other."""

import gzip
import hashlib
import importlib
import struct

import pytest
from fastapi.testclient import TestClient

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
recommendation: "HRV above baseline. Good day to push intensity."
---
"""

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


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Reloads app.config/app.storage/app.ingestion/app.routes/app.main so
    each test gets a fresh DATA_ROOT and DB file, rather than sharing the
    module-level singletons across tests."""
    monkeypatch.setenv("SYNC_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))

    import app.config
    import app.gui
    import app.ingestion
    import app.main
    import app.routes
    import app.security
    import app.storage

    importlib.reload(app.config)
    importlib.reload(app.security)
    importlib.reload(app.storage)
    importlib.reload(app.ingestion)
    importlib.reload(app.routes)
    importlib.reload(app.gui)
    importlib.reload(app.main)

    with TestClient(app.main.app) as client:
        yield client


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {SHARED_SECRET}"}


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
):
    import json

    envelope = {
        id_field: record_id,
        "relPath": rel_path,
        "contentHash": content_hash or content_hash_of(md_bytes),
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
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
):
    return _post_record(
        client,
        "/v1/readiness",
        "eventId",
        event_id,
        md_bytes,
        rel_path=rel_path or f"readiness/{event_id}.md",
        content_hash=content_hash,
        headers=headers,
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
