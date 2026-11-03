"""Kalshi API request signing, and a minimal .env reader.

Signed string: str(timestamp_ms) + METHOD + path, where path includes
/trade-api/v2 and excludes any query string. Ed25519 keys sign it directly;
RSA keys use PSS with SHA-256, MGF1-SHA-256 and salt length = digest length.
The key type comes from the parsed key, not the PEM header.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


def _decode(raw: bytes) -> str:
    """Notepad writes UTF-8 (sometimes with a BOM); PowerShell 5.1's > writes UTF-16."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = _decode(Path(path).read_bytes())
    except OSError:
        return out
    except UnicodeDecodeError:
        raise SystemExit(f"{Path(path).name} is not readable text: save it as UTF-8") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


class Signer:
    def __init__(self, key_id: str, key):
        self._key_id = key_id
        self._key = key

    @classmethod
    def from_pem(cls, key_id: str, pem_path: Path) -> "Signer":
        key = serialization.load_pem_private_key(Path(pem_path).read_bytes(), password=None)
        if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise ValueError("unsupported key type: use an Ed25519 or RSA key from Kalshi")
        return cls(key_id, key)

    def __repr__(self) -> str:            # never show the key id or key in logs
        return "Signer(<redacted>)"

    def headers(self, method: str, path: str, ts_ms: int | None = None) -> dict[str, str]:
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        msg = (ts + method.upper() + path.split("?", 1)[0]).encode()
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH),
                                 hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self._key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}
