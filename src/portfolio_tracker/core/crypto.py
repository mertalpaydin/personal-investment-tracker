"""Zero-knowledge AES-256-GCM encryption for static portfolio snapshots.

GitHub Pages serves every deployed file publicly. To host a personal portfolio
there without running ``run.py`` and without exposing a single ticker or euro
amount in the repository or on the CDN, the static builder pre-computes every
read-only API response into a JSON bundle, compresses it with gzip, and
encrypts it with **AES-256-GCM** using a key derived from the owner's secret
token via **PBKDF2-HMAC-SHA256** (600,000 iterations).

Why standard library + pure-Python AES-256-GCM:
1. The browser decrypts natively via the W3C Web Crypto API
   (``window.crypto.subtle.deriveKey`` + ``window.crypto.subtle.decrypt``) and
   ``DecompressionStream("gzip")`` with zero third-party JavaScript libraries,
   preserving the project's strict zero-CDN rule.
2. Keeping the Python side self-contained avoids adding C-extension
   dependencies to the core package while remaining 100% interoperable with
   NIST SP 800-38D and Web Crypto's ``AES-GCM``.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import secrets
from typing import Any

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12
TAG_BYTES = 16

# ---------------------------------------------------------------------------
# AES-256 (FIPS 197) + GCM (NIST SP 800-38D)
# ---------------------------------------------------------------------------

# fmt: off
_SBOX: tuple[int, ...] = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
# fmt: on

_RCON: tuple[int, ...] = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


# Precompute 32-bit T-tables for fast AES encryption of 16-byte blocks.
_T0: list[int] = [0] * 256
_T1: list[int] = [0] * 256
_T2: list[int] = [0] * 256
_T3: list[int] = [0] * 256

for _i in range(256):
    _s = _SBOX[_i]
    _s2 = _xtime(_s)
    _s3 = _s2 ^ _s
    _w = (_s2 << 24) | (_s << 16) | (_s << 8) | _s3
    _T0[_i] = _w
    _T1[_i] = ((_w << 24) | (_w >> 8)) & 0xFFFFFFFF
    _T2[_i] = ((_w << 16) | (_w >> 16)) & 0xFFFFFFFF
    _T3[_i] = ((_w << 8) | (_w >> 24)) & 0xFFFFFFFF


def _expand_key_256(key: bytes) -> list[int]:
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    w = [int.from_bytes(key[i : i + 4], "big") for i in range(0, 32, 4)]
    for i in range(8, 60):
        temp = w[i - 1]
        if i % 8 == 0:
            rotated = ((temp << 8) | (temp >> 24)) & 0xFFFFFFFF
            subbed = (
                (_SBOX[(rotated >> 24) & 0xFF] << 24)
                | (_SBOX[(rotated >> 16) & 0xFF] << 16)
                | (_SBOX[(rotated >> 8) & 0xFF] << 8)
                | _SBOX[rotated & 0xFF]
            )
            temp = subbed ^ (_RCON[i // 8] << 24)
        elif i % 8 == 4:
            temp = (
                (_SBOX[(temp >> 24) & 0xFF] << 24)
                | (_SBOX[(temp >> 16) & 0xFF] << 16)
                | (_SBOX[(temp >> 8) & 0xFF] << 8)
                | _SBOX[temp & 0xFF]
            )
        w.append(w[i - 8] ^ temp)
    return w


def _aes256_encrypt_block(block_int: int, rk: list[int]) -> int:
    s0 = ((block_int >> 96) & 0xFFFFFFFF) ^ rk[0]
    s1 = ((block_int >> 64) & 0xFFFFFFFF) ^ rk[1]
    s2 = ((block_int >> 32) & 0xFFFFFFFF) ^ rk[2]
    s3 = (block_int & 0xFFFFFFFF) ^ rk[3]

    idx = 4
    for _ in range(13):
        t0 = (
            _T0[(s0 >> 24) & 0xFF]
            ^ _T1[(s1 >> 16) & 0xFF]
            ^ _T2[(s2 >> 8) & 0xFF]
            ^ _T3[s3 & 0xFF]
            ^ rk[idx]
        )
        t1 = (
            _T0[(s1 >> 24) & 0xFF]
            ^ _T1[(s2 >> 16) & 0xFF]
            ^ _T2[(s3 >> 8) & 0xFF]
            ^ _T3[s0 & 0xFF]
            ^ rk[idx + 1]
        )
        t2 = (
            _T0[(s2 >> 24) & 0xFF]
            ^ _T1[(s3 >> 16) & 0xFF]
            ^ _T2[(s0 >> 8) & 0xFF]
            ^ _T3[s1 & 0xFF]
            ^ rk[idx + 2]
        )
        t3 = (
            _T0[(s3 >> 24) & 0xFF]
            ^ _T1[(s0 >> 16) & 0xFF]
            ^ _T2[(s1 >> 8) & 0xFF]
            ^ _T3[s2 & 0xFF]
            ^ rk[idx + 3]
        )
        s0, s1, s2, s3 = t0, t1, t2, t3
        idx += 4

    out0 = (
        (_SBOX[(s0 >> 24) & 0xFF] << 24)
        | (_SBOX[(s1 >> 16) & 0xFF] << 16)
        | (_SBOX[(s2 >> 8) & 0xFF] << 8)
        | _SBOX[s3 & 0xFF]
    ) ^ rk[56]
    out1 = (
        (_SBOX[(s1 >> 24) & 0xFF] << 24)
        | (_SBOX[(s2 >> 16) & 0xFF] << 16)
        | (_SBOX[(s3 >> 8) & 0xFF] << 8)
        | _SBOX[s0 & 0xFF]
    ) ^ rk[57]
    out2 = (
        (_SBOX[(s2 >> 24) & 0xFF] << 24)
        | (_SBOX[(s3 >> 16) & 0xFF] << 16)
        | (_SBOX[(s0 >> 8) & 0xFF] << 8)
        | _SBOX[s1 & 0xFF]
    ) ^ rk[58]
    out3 = (
        (_SBOX[(s3 >> 24) & 0xFF] << 24)
        | (_SBOX[(s0 >> 16) & 0xFF] << 16)
        | (_SBOX[(s1 >> 8) & 0xFF] << 8)
        | _SBOX[s2 & 0xFF]
    ) ^ rk[59]
    return (out0 << 96) | (out1 << 64) | (out2 << 32) | out3


def _ghash_table(h: int) -> list[int]:
    """Precompute 4-bit Shoup table for GF(2^128) multiplication by H."""
    # M[i] = h * i in GF(2^128) with reduction polynomial x^128 + x^7 + x^2 + x + 1
    m = [0] * 16
    v = h
    for bit in (8, 4, 2, 1):
        m[bit] = v
        lsb = v & 1
        v >>= 1
        if lsb:
            v ^= 0xE1000000000000000000000000000000
    for i in range(2, 16):
        if i not in (2, 4, 8):
            low_bit = i & -i
            m[i] = m[low_bit] ^ m[i ^ low_bit]
    return m


# 4-bit reduction table for fast GF(2^128) shift-by-4
_RED4: tuple[int, ...] = tuple(
    (
        (0xE1000000000000000000000000000000 >> 3 if (i & 1) else 0)
        ^ (0xE1000000000000000000000000000000 >> 2 if (i & 2) else 0)
        ^ (0xE1000000000000000000000000000000 >> 1 if (i & 4) else 0)
        ^ (0xE1000000000000000000000000000000 if (i & 8) else 0)
    )
    for i in range(16)
)


def _ghash_mul(x: int, m: list[int]) -> int:
    z = 0
    for _ in range(32):
        nibble = x & 0xF
        x >>= 4
        rem = z & 0xF
        z = (z >> 4) ^ _RED4[rem] ^ m[nibble]
    return z


def _ghash(h: int, ciphertext: bytes) -> int:
    m = _ghash_table(h)
    y = 0
    n = len(ciphertext)
    full_end = (n // 16) * 16
    for offset in range(0, full_end, 16):
        block = int.from_bytes(ciphertext[offset : offset + 16], "big")
        y = _ghash_mul(y ^ block, m)
    if n > full_end:
        rem_bytes = ciphertext[full_end:].ljust(16, b"\x00")
        y = _ghash_mul(y ^ int.from_bytes(rem_bytes, "big"), m)
    # Length block: 64-bit AAD bit-length (0) || 64-bit ciphertext bit-length
    len_block = n * 8
    y = _ghash_mul(y ^ len_block, m)
    return y


def _ctr_crypt(rk: list[int], j0: int, data: bytes) -> bytes:
    out = bytearray(len(data))
    prefix = j0 & 0xFFFFFFFFFFFFFFFFFFFFFFFF00000000
    counter = j0 & 0xFFFFFFFF
    n = len(data)
    for offset in range(0, n, 16):
        counter = (counter + 1) & 0xFFFFFFFF
        cb = prefix | counter
        ks = _aes256_encrypt_block(cb, rk).to_bytes(16, "big")
        chunk = data[offset : offset + 16]
        for i, b in enumerate(chunk):
            out[offset + i] = b ^ ks[i]
    return bytes(out)


def aes256_gcm_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` with AES-256-GCM and return ``ciphertext || 16-byte tag``."""
    if len(iv) != IV_BYTES:
        raise ValueError(f"GCM IV must be {IV_BYTES} bytes")
    rk = _expand_key_256(key)
    h = _aes256_encrypt_block(0, rk)
    j0 = (int.from_bytes(iv, "big") << 32) | 1
    ciphertext = _ctr_crypt(rk, j0, plaintext)
    s = _ghash(h, ciphertext)
    tag = (_aes256_encrypt_block(j0, rk) ^ s).to_bytes(16, "big")
    return ciphertext + tag


def aes256_gcm_decrypt(key: bytes, iv: bytes, ciphertext_and_tag: bytes) -> bytes:
    """Verify and decrypt ``ciphertext || 16-byte tag`` with AES-256-GCM."""
    if len(iv) != IV_BYTES:
        raise ValueError(f"GCM IV must be {IV_BYTES} bytes")
    if len(ciphertext_and_tag) < TAG_BYTES:
        raise ValueError("Ciphertext too short to contain GCM tag")
    rk = _expand_key_256(key)
    h = _aes256_encrypt_block(0, rk)
    j0 = (int.from_bytes(iv, "big") << 32) | 1
    ciphertext = ciphertext_and_tag[:-TAG_BYTES]
    tag = ciphertext_and_tag[-TAG_BYTES:]
    s = _ghash(h, ciphertext)
    expected_tag = (_aes256_encrypt_block(j0, rk) ^ s).to_bytes(16, "big")
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Invalid token or corrupted encrypted snapshot")
    return _ctr_crypt(rk, j0, ciphertext)


# ---------------------------------------------------------------------------
# High-level snapshot envelope (interoperable with browser Web Crypto API)
# ---------------------------------------------------------------------------


def derive_key(token: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Derive a 256-bit AES key from ``token`` using PBKDF2-HMAC-SHA256."""
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Access token must not be empty")
    return hashlib.pbkdf2_hmac("sha256", cleaned.encode("utf-8"), salt, iterations, dklen=32)


def encrypt_snapshot(
    payload: dict[str, Any],
    token: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
) -> dict[str, Any]:
    """Compress and encrypt a snapshot dictionary into a JSON-serializable envelope."""
    raw_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw_json, compresslevel=9)
    salt = secrets.token_bytes(SALT_BYTES)
    iv = secrets.token_bytes(IV_BYTES)
    key = derive_key(token, salt, iterations=iterations)
    ct_and_tag = aes256_gcm_encrypt(key, iv, compressed)
    return {
        "v": 1,
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iter": iterations,
        "compression": "gzip",
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct_and_tag).decode("ascii"),
    }


def decrypt_snapshot(envelope: dict[str, Any], token: str) -> dict[str, Any]:
    """Decrypt and decompress a snapshot envelope back into its original dict."""
    if envelope.get("v") != 1 or envelope.get("alg") != "AES-256-GCM":
        raise ValueError("Unsupported encrypted snapshot format")
    salt = base64.b64decode(str(envelope["salt"]))
    iv = base64.b64decode(str(envelope["iv"]))
    ct_and_tag = base64.b64decode(str(envelope["ct"]))
    iterations = int(envelope.get("iter", PBKDF2_ITERATIONS))
    key = derive_key(token, salt, iterations=iterations)
    compressed = aes256_gcm_decrypt(key, iv, ct_and_tag)
    raw_json = gzip.decompress(compressed) if envelope.get("compression") == "gzip" else compressed
    result: dict[str, Any] = json.loads(raw_json.decode("utf-8"))
    return result


def encrypt_bytes(data: bytes, token: str, *, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Compress and encrypt arbitrary bytes (e.g. a SQLite database backup for CI)."""
    compressed = gzip.compress(data, compresslevel=9)
    salt = secrets.token_bytes(SALT_BYTES)
    iv = secrets.token_bytes(IV_BYTES)
    key = derive_key(token, salt, iterations=iterations)
    ct_and_tag = aes256_gcm_encrypt(key, iv, compressed)
    envelope = {
        "v": 1,
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iter": iterations,
        "compression": "gzip",
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct_and_tag).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decrypt_bytes(envelope_bytes: bytes, token: str) -> bytes:
    """Decrypt and decompress bytes encrypted with ``encrypt_bytes``."""
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    if envelope.get("v") != 1 or envelope.get("alg") != "AES-256-GCM":
        raise ValueError("Unsupported encrypted format")
    salt = base64.b64decode(str(envelope["salt"]))
    iv = base64.b64decode(str(envelope["iv"]))
    ct_and_tag = base64.b64decode(str(envelope["ct"]))
    iterations = int(envelope.get("iter", PBKDF2_ITERATIONS))
    key = derive_key(token, salt, iterations=iterations)
    compressed = aes256_gcm_decrypt(key, iv, ct_and_tag)
    if envelope.get("compression") == "gzip":
        return gzip.decompress(compressed)
    return compressed
