from __future__ import annotations
import os
import json
import hmac
import hashlib
import base64
import time
import warnings
from typing import Any
from aaac.common.classes import AccessClass, variant_for

class TokenError(Exception):
    pass

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip("=")

def _b64url_decode(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)

def _get_secret() -> bytes:
    secret = os.environ.get("AAAC_TOKEN_SECRET", "")
    if not secret:
        warnings.warn(
            "AAAC_TOKEN_SECRET is not set — using insecure dev fallback. "
            "Set this env var before running in production.",
            stacklevel=3,
        )
        secret = "dev_secret"
    return secret.encode("utf-8")

def issue_token(tid: str, cls: AccessClass, attempt: int, ttl_s: float) -> str:
    """Issue a compact HMAC-SHA256 admit token."""
    payload = {
        "tid": tid,
        "cls": int(cls),
        "att": attempt,
        "exp": time.time() + ttl_s,
        "var": variant_for(cls)
    }
    payload_json = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    b64_payload = _b64url_encode(payload_json)
    
    secret = _get_secret()
    sig = hmac.new(secret, b64_payload.encode('utf-8'), hashlib.sha256).digest()
    b64_sig = _b64url_encode(sig)
    
    return f"{b64_payload}.{b64_sig}"

def verify_token(raw: str) -> dict[str, Any]:
    """Verify and parse an admit token. Raises TokenError."""
    try:
        b64_payload, b64_sig = raw.split(".")
    except ValueError:
        raise TokenError("Invalid token format")
        
    secret = _get_secret()
    expected_sig = hmac.new(secret, b64_payload.encode('utf-8'), hashlib.sha256).digest()
    
    try:
        sig = _b64url_decode(b64_sig)
    except Exception:
        raise TokenError("Invalid signature encoding")
        
    if not hmac.compare_digest(sig, expected_sig):
        raise TokenError("Invalid token signature")
        
    try:
        payload_json = _b64url_decode(b64_payload)
        payload = json.loads(payload_json)
    except Exception:
        raise TokenError("Invalid payload encoding")
        
    if payload.get("exp", 0) < time.time():
        raise TokenError("Token expired")
        
    return payload
