"""Google OAuth helpers for per-user Calendar access."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response
from google_auth_oauthlib.flow import Flow

from app.config import settings
from app import storage

AUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
SESSION_COOKIE = "pca_session"
SESSION_DAYS = 30


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _secret() -> bytes:
    secret = settings.app_secret_key or settings.google_oauth_client_secret
    if not secret:
        raise HTTPException(500, "APP_SECRET_KEY or GOOGLE_OAUTH_CLIENT_SECRET is required")
    return secret.encode("utf-8")


def _sign_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64(raw)
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def _verify_payload(token: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_unb64(body))
        exp = payload.get("exp")
        if exp and datetime.now(timezone.utc).timestamp() > float(exp):
            return None
        return payload
    except Exception:
        return None


def _oauth_client_config() -> dict:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(500, "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET are required")
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _redirect_uri(request: Request) -> str:
    return settings.google_oauth_redirect_uri or str(request.url_for("auth_google_callback"))


def _flow(request: Request) -> Flow:
    redirect_uri = _redirect_uri(request)
    if redirect_uri.startswith(("http://localhost", "http://127.0.0.1")):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    return Flow.from_client_config(
        _oauth_client_config(),
        scopes=AUTH_SCOPES,
        redirect_uri=redirect_uri,
        autogenerate_code_verifier=False,
    )


def build_google_auth_url(request: Request) -> str:
    state = _sign_payload({
        "nonce": _b64(os.urandom(18)),
        "exp": (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp(),
    })
    auth_url, _ = _flow(request).authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return auth_url


def exchange_google_callback(request: Request, code: str, state: str) -> dict:
    if not _verify_payload(state):
        raise HTTPException(400, "Invalid OAuth state")

    flow = _flow(request)
    flow.fetch_token(code=code)
    creds = flow.credentials

    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        resp.raise_for_status()
        profile = resp.json()

    email = profile.get("email")
    if not email:
        raise HTTPException(400, "Google did not return an email address")

    existing = storage.get_oauth_user(email)
    existing_refresh = ((existing or {}).get("token") or {}).get("refresh_token")
    token = {
        "token": creds.token,
        "refresh_token": creds.refresh_token or existing_refresh,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or AUTH_SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    storage.upsert_oauth_user(
        email=email,
        name=profile.get("name") or email,
        picture=profile.get("picture") or "",
        token=token,
    )
    return {"email": email, "name": profile.get("name") or email, "picture": profile.get("picture") or ""}


def set_session_cookie(response: Response, user: dict, request: Request) -> None:
    payload = {
        "email": user["email"],
        "name": user.get("name") or user["email"],
        "picture": user.get("picture") or "",
        "exp": (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).timestamp(),
    }
    response.set_cookie(
        SESSION_COOKIE,
        _sign_payload(payload),
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def get_session_user(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    payload = _verify_payload(raw)
    if not payload or not payload.get("email"):
        return None
    return {
        "email": payload["email"],
        "name": payload.get("name") or payload["email"],
        "picture": payload.get("picture") or "",
    }


def require_session_user(request: Request) -> dict:
    user = get_session_user(request)
    if not user:
        raise HTTPException(401, "Sign in with Google to read your calendar")
    return user
