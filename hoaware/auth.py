"""Authentication utilities: password hashing, JWT creation/validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from passlib.context import CryptContext

from hoaware import db
from hoaware.config import Settings, load_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(
    user_id: int,
    settings: Settings | None = None,
) -> tuple[str, str, datetime]:
    """Return (token, jti, expires_at)."""
    if settings is None:
        settings = load_settings()
    jti = uuid.uuid4().hex
    expires = datetime.now(timezone.utc) + timedelta(days=settings.jwt_expiry_days)
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "exp": expires,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, expires


def decode_token(token: str, settings: Settings | None = None) -> dict:
    """Decode and validate a JWT. Returns the payload dict."""
    if settings is None:
        settings = load_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    return payload


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract and validate the current user from the
    Authorization header. Returns the user dict from the database."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    token = auth_header[7:]
    settings = load_settings()
    payload = decode_token(token, settings)
    jti = payload.get("jti")
    user_id = payload.get("sub")
    if not jti or not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    conn = db.get_connection(settings.db_path)
    # Verify session still exists (not logged out)
    session = db.get_session_by_jti(conn, jti)
    if not session:
        raise HTTPException(status_code=401, detail="Session has been revoked")

    user = db.get_user_by_id(conn, int(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Attach jti so logout can revoke it
    user["_jti"] = jti
    return user


def optional_current_user(request: Request) -> dict | None:
    """Like get_current_user but returns None instead of raising for
    unauthenticated requests."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    try:
        return get_current_user(request)
    except HTTPException:
        return None
