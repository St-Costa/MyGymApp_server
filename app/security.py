"""Bearer-token auth.

Single shared secret, single user (see docs/sync-ingestion/SPEC.md — "one
phone, one server, both owned by the same person"). No accounts, no scopes.
The only thing worth getting right here is the constant-time comparison.
"""

import hmac

from fastapi import Header, HTTPException

from app import config


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
