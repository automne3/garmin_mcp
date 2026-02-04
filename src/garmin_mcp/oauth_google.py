"""Google OAuth token validation utilities for HTTP-based MCP servers."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import requests


class GoogleOAuthValidator:
    """Validate Google OAuth access tokens via tokeninfo with caching."""

    def __init__(self, client_id: str, cache_ttl_seconds: int = 600) -> None:
        if not client_id:
            raise ValueError("Google OAuth client_id is required")
        self._client_id = client_id
        self._cache_ttl_seconds = max(30, cache_ttl_seconds)
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def validate_token(self, access_token: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Validate access token and return (ok, payload, error_message)."""
        if not access_token:
            return False, None, "Missing access token"

        now = time.time()
        cached = self._cache.get(access_token)
        if cached:
            exp_ts, payload = cached
            if exp_ts > now:
                return True, payload, ""
            self._cache.pop(access_token, None)

        try:
            response = requests.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
                timeout=5,
            )
        except requests.RequestException as exc:
            return False, None, f"Token validation failed: {exc}"

        if response.status_code != 200:
            return False, None, "Invalid access token"

        payload = response.json()
        aud = payload.get("aud") or payload.get("issued_to")
        if aud != self._client_id:
            return False, None, "Token audience mismatch"

        exp_ts = self._extract_expiry(payload, now)
        if exp_ts <= now:
            return False, None, "Access token expired"

        cache_ttl = min(exp_ts - now, self._cache_ttl_seconds)
        self._cache[access_token] = (now + cache_ttl, payload)
        return True, payload, ""

    @staticmethod
    def _extract_expiry(payload: Dict[str, Any], now: float) -> float:
        exp = payload.get("exp")
        if exp is not None:
            try:
                return float(exp)
            except (TypeError, ValueError):
                return now
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            try:
                return now + float(expires_in)
            except (TypeError, ValueError):
                return now
        return now


def extract_bearer_token(authorization_header: str | None) -> str:
    """Extract Bearer token from Authorization header."""
    if not authorization_header:
        return ""
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()
