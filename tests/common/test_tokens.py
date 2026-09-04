import os
import pytest
import time
from aaac.common.classes import AccessClass
from aaac.common.tokens import issue_token, verify_token, TokenError

def test_token_round_trip():
    os.environ["AAAC_TOKEN_SECRET"] = "test_secret"
    tid = "t_123"
    cls = AccessClass.HIGH
    att = 1
    ttl_s = 60.0
    
    token = issue_token(tid, cls, att, ttl_s)
    payload = verify_token(token)
    
    assert payload["tid"] == tid
    assert payload["cls"] == int(cls)
    assert payload["att"] == att
    assert payload["var"] == "full"
    assert payload["exp"] > time.time()

def test_token_expired():
    token = issue_token("t_123", AccessClass.LOW, 1, -10.0) # expired 10s ago
    with pytest.raises(TokenError, match="Token expired"):
        verify_token(token)

def test_token_tampered():
    token = issue_token("t_123", AccessClass.HIGH, 1, 60.0)
    payload, sig = token.split(".")
    
    # Tamper payload
    tampered_token = f"{payload}X.{sig}"
    with pytest.raises(TokenError):
        verify_token(tampered_token)
        
    # Tamper sig
    tampered_sig_token = f"{payload}.{sig}X"
    with pytest.raises(TokenError):
        verify_token(tampered_sig_token)
