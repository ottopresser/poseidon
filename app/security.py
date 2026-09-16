from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException, Query, status


def _verify_token(
    token: str | None,
    env_var: str,
    missing_detail: str,
    invalid_detail: str,
) -> None:
    expected = os.getenv(env_var, "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=missing_detail,
        )

    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=invalid_detail,
        )


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    _verify_token(
        token=x_api_key,
        env_var="POSEIDON_API_KEY",
        missing_detail="POSEIDON_API_KEY is not configured on this server.",
        invalid_detail="Invalid API key.",
    )


def verify_admin_token(
    x_admin_token: str | None = Header(default=None),
    admin_token: str | None = Query(default=None),
) -> None:
    _verify_token(
        token=x_admin_token or admin_token,
        env_var="POSEIDON_ADMIN_TOKEN",
        missing_detail="POSEIDON_ADMIN_TOKEN is not configured on this server.",
        invalid_detail="Invalid admin token.",
    )


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _console_token_secret() -> str:
    secret = os.getenv("POSEIDON_CONSOLE_TOKEN_SECRET", "")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POSEIDON_CONSOLE_TOKEN_SECRET is not configured on this server.",
        )
    return secret


def get_console_token_ttl_seconds() -> int:
    default_ttl = 60
    raw = os.getenv("POSEIDON_CONSOLE_TOKEN_TTL_SECONDS", str(default_ttl)).strip()
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POSEIDON_CONSOLE_TOKEN_TTL_SECONDS must be an integer.",
        ) from exc

    return validate_console_token_ttl_seconds(ttl)


def validate_console_token_ttl_seconds(ttl: int) -> int:
    if ttl < 5 or ttl > 3600:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="POSEIDON_CONSOLE_TOKEN_TTL_SECONDS must be between 5 and 3600.",
        )

    return ttl


def issue_console_token(vm_name: str, ttl_seconds: int | None = None) -> tuple[str, int, int]:
    secret = _console_token_secret()
    ttl = validate_console_token_ttl_seconds(ttl_seconds) if ttl_seconds is not None else get_console_token_ttl_seconds()
    now = int(time.time())
    expires_at = now + ttl

    payload_obj = {"vm": vm_name, "iat": now, "exp": expires_at}
    payload_json = json.dumps(payload_obj, separators=(",", ":"), sort_keys=True)
    payload_b64 = _base64url_encode(payload_json.encode("utf-8"))

    signature = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        digestmod=hashlib.sha256,
    ).digest()
    signature_b64 = _base64url_encode(signature)
    token = f"{payload_b64}.{signature_b64}"
    return token, ttl, expires_at


def verify_console_token(token: str | None, vm_name: str) -> None:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing console token.",
        )

    secret = _console_token_secret()
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid console token format.",
        ) from exc

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        payload_b64.encode("ascii"),
        digestmod=hashlib.sha256,
    ).digest()
    try:
        presented_sig = _base64url_decode(signature_b64)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid console token signature encoding.",
        ) from exc
    if not hmac.compare_digest(presented_sig, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid console token signature.",
        )

    try:
        payload_raw = _base64url_decode(payload_b64)
        payload_obj = json.loads(payload_raw.decode("utf-8"))
        payload_vm = str(payload_obj["vm"])
        payload_exp = int(payload_obj["exp"])
    except (KeyError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid console token payload.",
        ) from exc

    if not hmac.compare_digest(payload_vm, vm_name):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Console token is not valid for this VM.",
        )

    now = int(time.time())
    if now >= payload_exp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Console token has expired.",
        )
