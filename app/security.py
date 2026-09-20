"""Bearer-token auth.

Single shared secret, single user (see docs/sync-ingestion/SPEC.md — "one
phone, one server, both owned by the same person"). No accounts, no scopes.
The only thing worth getting right here is the constant-time comparison.
"""

import hmac
import logging
import secrets

from fastapi import Header, HTTPException

from app import config

logger = logging.getLogger("mygymapp.security")


def require_valid_bearer_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401s the request unless the bearer token matches.

    Uses hmac.compare_digest instead of `==` so a wrong guess can't be
    distinguished from a correct one by response-time side channel. Reads
    `config.SYNC_SHARED_SECRET` through the module (not a bound import) so a
    value read at process startup can't go stale relative to the module.
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")

    secret = config.SYNC_SHARED_SECRET
    if not secret or not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail={"error": "Unauthorized"})


def generate_token() -> str:
    """A fresh shared secret: 256 bits of entropy, hex-encoded (64 chars) —
    same shape as the `openssl rand -hex 32` value .env.example suggests."""
    return secrets.token_hex(32)


def rotate_shared_secret() -> str:
    """Generates a new secret, activates it immediately (in-memory) and
    persists it to config.SECRET_FILE (mode 600) so it survives restarts —
    including inside Docker, where the host `.env` isn't writable from the
    container. Deliberately the *only* write target (never the process cwd's
    `.env`): tests run with the repo root as cwd, so touching `.env` would
    clobber the real secret on every test run. Returns the new secret
    (shown once in the GUI — never logged)."""
    new_secret = generate_token()
    config.SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SECRET_FILE.write_text(new_secret + "\n", encoding="utf-8")
    try:
        config.SECRET_FILE.chmod(0o600)
    except OSError:
        logger.warning("could not chmod %s to 600", config.SECRET_FILE)
    config.SYNC_SHARED_SECRET = new_secret
    return new_secret
