"""FastAPI app entrypoint.

Placeholder skeleton — routes (POST /v1/sessions, GET /health), parsing, and
storage logic are implemented per docs/sync-ingestion/SPEC.md.
"""

from fastapi import FastAPI

app = FastAPI(title="MyGymApp Sync Server")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
