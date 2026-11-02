import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kxmm.auth import Signer, load_env

MSG = b"1700000000000GET/trade-api/v2/portfolio/balance"
PATH = "/trade-api/v2/portfolio/balance?x=1"


def write_key(tmp_path, key, fmt):
    pem = key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())
    p = tmp_path / "key.pem"
    p.write_bytes(pem)
    return p


def test_ed25519_signature_verifies(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    s = Signer.from_pem("kid", write_key(tmp_path, key, serialization.PrivateFormat.PKCS8))
    h = s.headers("GET", PATH, ts_ms=1700000000000)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), MSG)


def test_rsa_pss_signature_verifies(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = Signer.from_pem("kid", write_key(tmp_path, key,
                                         serialization.PrivateFormat.TraditionalOpenSSL))
    h = s.headers("GET", PATH, ts_ms=1700000000000)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), MSG,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                            hashes.SHA256())


def test_headers_names(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    s = Signer.from_pem("kid", write_key(tmp_path, key, serialization.PrivateFormat.PKCS8))
    h = s.headers("GET", PATH, ts_ms=1700000000000)
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"


def test_load_env_parses_and_ignores_comments(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# demo keys\nKALSHI_DEMO_KEY_ID=abc-123\n\nKALSHI_DEMO_KEY_PATH="C:\\keys\\demo.pem"\n'
                 "BROKEN LINE\nQUOTED='x y'\n", encoding="utf-8")
    assert load_env(p) == {"KALSHI_DEMO_KEY_ID": "abc-123",
                           "KALSHI_DEMO_KEY_PATH": "C:\\keys\\demo.pem", "QUOTED": "x y"}
    assert load_env(tmp_path / "missing.env") == {}
