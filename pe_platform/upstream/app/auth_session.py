from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Optional

import jwt
from fastapi import Response

from .config import settings
from .models import Organization, OrganizationMembership, User


SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 7 * 24 * 60


@dataclass
class AuthContext:
    user: User
    organization: Optional[Organization]
    membership: Optional[OrganizationMembership]
    auth_source: str
    token: str
    stytch_session: Optional[dict[str, Any]] = None


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=settings.auth_session_minutes * 60,
        path="/",
        domain=settings.auth_cookie_domain or None,
    )


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        httponly=False,
        secure=True,
        samesite="none",
        max_age=settings.auth_session_minutes * 60,
        path="/",
        domain=settings.auth_cookie_domain or None,
    )


def _delete_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        domain=settings.auth_cookie_domain or None,
        secure=True,
        httponly=True,
        samesite="none",
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        domain=settings.auth_cookie_domain or None,
        secure=True,
        httponly=False,
        samesite="none",
    )


def _auth_response_payload(
    payload: dict[str, Any],
    token: str,
    csrf_token: Optional[str] = None,
) -> dict[str, Any]:
    return {
        **payload,
        "token_type": "cookie",
        "expires_in": settings.auth_session_minutes * 60,
        **({"csrf_token": csrf_token} if csrf_token else {}),
    }
