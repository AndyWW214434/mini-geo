"""WorkOS AuthKit session verification, shared by the REST API and the MCP server.

The Next.js app holds the WorkOS session cookie and forwards the user's access token (a
JWT signed by WorkOS) as a bearer token. Every backend request verifies the signature
against WorkOS's JWKS and its expiry. The organization comes from the token's `org_id`
claim, never from the request body, so a user can only ever act inside their own org.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import jwt
from fastapi import Depends, HTTPException, Request, status

from geo.config import settings


@dataclass(frozen=True)
class Principal:
    user_id: str
    org_id: str | None
    role: str | None
    session_id: str | None
    token: str
    expires_at: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class InvalidToken(Exception):
    pass


class Verifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class WorkOSVerifier:
    def __init__(self, client_id: str, leeway: int = 30):
        self._jwks = jwt.PyJWKClient(f"https://api.workos.com/sso/jwks/{client_id}", cache_keys=True,
                                     lifespan=3600)
        self._leeway = leeway

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], leeway=self._leeway,
                                options={"require": ["exp", "sub"]})
        except jwt.ExpiredSignatureError as exc:
            raise InvalidToken("session expired") from exc
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        return principal_from_claims(claims, token)


def principal_from_claims(claims: dict[str, Any], token: str) -> Principal:
    return Principal(user_id=claims["sub"], org_id=claims.get("org_id"), role=claims.get("role"),
                     session_id=claims.get("sid"), token=token, expires_at=claims.get("exp"))


_override: Verifier | None = None


def set_verifier(v: Verifier | None) -> None:
    """Tests install a verifier backed by a local key pair."""
    global _override
    _override = v


@lru_cache
def _workos_verifier() -> WorkOSVerifier:
    if not settings().workos_client_id:
        raise RuntimeError("WORKOS_CLIENT_ID is not set")
    return WorkOSVerifier(settings().workos_client_id)


def verifier() -> Verifier:
    return _override or _workos_verifier()


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    return token


def current_principal(token: str = Depends(bearer)) -> Principal:
    try:
        return verifier().verify(token)
    except InvalidToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def org_member(p: Principal = Depends(current_principal)) -> Principal:
    if not p.org_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no organization selected; create or join one first")
    return p


def org_admin(p: Principal = Depends(org_member)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")
    return p
