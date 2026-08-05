"""Shared fixtures: isolate every test behind its own temp DATA_ROOT/secret so
tests never touch a real deployment's data or share state with each other."""

import hashlib
import importlib

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

SHARED_SECRET = "test-secret-for-unit-tests"


def content_hash_of(raw_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Reloads app.config/app.storage/app.ingestion/app.routes/app.main so
    each test gets a fresh DATA_ROOT and DB file, rather than sharing the
    module-level singletons across tests."""
    monkeypatch.setenv("SYNC_SHARED_SECRET", SHARED_SECRET)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))

    import app.config
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
    importlib.reload(app.main)

    with TestClient(app.main.app) as client:
        yield client


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {SHARED_SECRET}"}


def post_session(
    client,
    md_bytes: bytes = SESSION_MD.encode(),
    *,
    session_id="3c4d5e6f",
    rel_path=None,
    content_hash=None,
    headers=None,
):
    envelope = {
        "sessionId": session_id,
        "relPath": rel_path or f"2025/04/2025-04-15_rt-b2c3d4e5_{session_id}.md",
        "contentHash": content_hash or content_hash_of(md_bytes),
        "appVersion": "1.0-test",
        "clientSentAt": "2025-04-15T10:05:42Z",
    }
    import json

    return client.post(
        "/v1/sessions",
        data={"envelope": json.dumps(envelope)},
        files={"file": ("session.md", md_bytes, "text/markdown")},
        headers=headers if headers is not None else auth_headers(),
    )
