"""JWT identity (HS256). The role claim is authoritative on every request; there is no default secret."""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import jwt
from fastapi import Header, HTTPException

from app.core.config import settings

ROLES = {"user", "super", "admin"}


class AuthError(Exception):
    status_code = 401

    def __init__(self, message: str = "invalid credentials"):
        super().__init__(message)


def _secret(secret: Optional[str]) -> str:
    s = secret or settings.JWT_SECRET
    if not s:
        raise AuthError("JWT secret is not configured")
    return s


def create_token(user_id: str, role: str = "user", ttl_seconds: int = 3600, secret: Optional[str] = None) -> str:
    if role not in ROLES:
        raise AuthError(f"unknown role: {role}")
    if not user_id:
        raise AuthError("user_id required")
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": now + timedelta(seconds=ttl_seconds)}
    return jwt.encode(payload, _secret(secret), algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, secret: Optional[str] = None) -> Dict[str, str]:
    key = _secret(secret)
    if not token or not token.strip():
        raise AuthError("empty token")
    try:
        claims = jwt.decode(token, key, algorithms=[settings.JWT_ALGORITHM],
                            options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e
    role = claims.get("role", "user")
    if role not in ROLES or not claims.get("sub"):
        raise AuthError("invalid claims")
    return {"user_id": str(claims["sub"]), "role": role}


def get_identity(authorization: Optional[str] = Header(None), x_user_id: Optional[str] = Header(None),
                 x_user_role: Optional[str] = Header(None)) -> Dict[str, str]:
    """Bearer token (must be valid if sent) > dev headers (ALLOW_HEADER_IDENTITY) > anonymous (unless AUTH_REQUIRED)."""
    authorization = authorization if isinstance(authorization, str) else None  # direct calls leave Header() defaults
    x_user_id = x_user_id if isinstance(x_user_id, str) else None
    x_user_role = x_user_role if isinstance(x_user_role, str) else None
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="invalid authorization scheme")
        try:
            return decode_token(token)
        except AuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
    if settings.ALLOW_HEADER_IDENTITY and x_user_id:
        return {"user_id": x_user_id, "role": x_user_role if x_user_role in ROLES else "user"}
    if settings.AUTH_REQUIRED:
        raise HTTPException(status_code=401, detail="authentication required")
    return {"user_id": "anonymous", "role": "user"}
