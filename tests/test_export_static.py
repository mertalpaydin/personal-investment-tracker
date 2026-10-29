"""Tests for AES-256-GCM encryption and the GitHub Pages static snapshot builder."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_tracker.core import crypto, rebuild
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.export_static import (
    build_static_site,
    normalize_endpoint_key,
)


def test_aes256_gcm_matches_nist_sp800_38d_vectors() -> None:
    """Verify bit-for-bit compliance with NIST SP 800-38D Test Cases 15 and 16."""
    # Test Case 15: 16-byte zero plaintext
    key15 = bytes(32)
    iv15 = bytes(12)
    pt15 = bytes(16)
    out15 = crypto.aes256_gcm_encrypt(key15, iv15, pt15)
    assert out15[:16] == bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18")
    assert out15[16:] == bytes.fromhex("d0d1c8a799996bf0265b98b5d48ab919")
    assert crypto.aes256_gcm_decrypt(key15, iv15, out15) == pt15

    # Test Case 16: 64-byte multi-block plaintext
    key16 = bytes.fromhex("feffe9928665731c6d6a8f9467308308feffe9928665731c6d6a8f9467308308")
    iv16 = bytes.fromhex("cafebabefacedbaddecaf888")
    pt16 = bytes.fromhex(
        "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
        "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255"
    )
    out16 = crypto.aes256_gcm_encrypt(key16, iv16, pt16)
    assert out16[:64] == bytes.fromhex(
        "522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
        "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662898015ad"
    )
    assert out16[64:] == bytes.fromhex("b094dac5d93471bdec1a502270e3cc6c")
    assert crypto.aes256_gcm_decrypt(key16, iv16, out16) == pt16


def test_tampered_ciphertext_or_wrong_token_is_rejected() -> None:
    payload = {"secret_holding": "SEC0", "value_eur": 123456.78}
    envelope = crypto.encrypt_snapshot(payload, "correct-horse-battery-staple", iterations=1000)

    with pytest.raises(ValueError, match="Invalid token"):
        crypto.decrypt_snapshot(envelope, "wrong-token")

    # Flip one bit in the ciphertext
    import base64

    raw_ct = bytearray(base64.b64decode(envelope["ct"]))
    raw_ct[0] ^= 0x01
    tampered = {**envelope, "ct": base64.b64encode(bytes(raw_ct)).decode("ascii")}
    with pytest.raises(ValueError, match="Invalid token"):
        crypto.decrypt_snapshot(tampered, "correct-horse-battery-staple")


def test_normalize_endpoint_key_sorts_and_formats_booleans() -> None:
    assert normalize_endpoint_key("/api/watchlist", {"metrics": True, "all": True}) == (
        "/api/watchlist?all=true&metrics=true"
    )
    assert normalize_endpoint_key("/api/overview", {"range": "1M", "empty": None}) == (
        "/api/overview?range=1M"
    )


def test_build_static_site_produces_zero_plaintext_and_decrypts_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    conn = connect(db_path)
    M.migrate(conn)
    M.seed_default_owner(conn)
    conn.execute(
        "INSERT INTO instrument (id, import_key, name, asset_class, currency, is_fund, "
        "price_source, status, risk_bucket, risk_level) "
        "VALUES (1, 'SEC0', 'Synthetic World ETF', 'ETF', 'EUR', 1, 'manual', 'OWNED', "
        "'EQ_GLOBAL_BROAD', 4)"
    )
    conn.execute(
        "INSERT INTO transaction_ (account_id, instrument_id, type, trade_date, quantity, "
        "price_per_unit_local, gross_amount_local, gross_amount_eur, currency, fx_rate, "
        "confidence, flow_class, source) "
        "VALUES (1, 1, 'BUY', '2026-01-05', '10', '100', '1000', '1000', 'EUR', '1', "
        "'ACTUAL', 'INVESTMENT', 'MANUAL')"
    )
    for day in ("2026-01-05", "2026-01-06", "2026-01-07"):
        conn.execute(
            "INSERT INTO price (instrument_id, date, close, total_return_close, currency, "
            "source, fetched_at) VALUES (1, ?, '105', '105', 'EUR', 'manual', '2026-01-07T00:00:00Z')",
            (day,),
        )
    conn.commit()
    rebuild.run(conn, end=date(2026, 1, 7))
    conn.close()

    out_dir = tmp_path / "dist"
    token = "test-secret-token-99"
    snap_path = build_static_site(db_path, out_dir, token, iterations=1000)

    assert snap_path.is_file()
    assert (out_dir / "index.html").is_file()
    assert (out_dir / ".nojekyll").is_file()
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "vault.enc").is_file()

    # Relative asset paths for GitHub Pages subpath compatibility
    index_html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="./static/app.css' in index_html
    assert 'src="./static/js/main.js' in index_html

    # Zero plaintext financial data in the encrypted files
    raw_snap_text = snap_path.read_text(encoding="utf-8")
    assert "SEC0" not in raw_snap_text
    assert "Synthetic World ETF" not in raw_snap_text
    assert "1050" not in raw_snap_text

    raw_vault_text = (out_dir / "vault.enc").read_text(encoding="utf-8")
    assert "SEC0" not in raw_vault_text
    assert "Synthetic World ETF" not in raw_vault_text

    # Decrypt snapshot and verify key endpoints are present
    envelope = json.loads(raw_snap_text)
    decrypted = crypto.decrypt_snapshot(envelope, token)
    endpoints = decrypted["endpoints"]
    assert "/api/portfolio" in endpoints
    assert endpoints["/api/portfolio"]["__ok__"] is True
    assert endpoints["/api/portfolio"]["body"]["total_value_eur"] == pytest.approx(1050.0)
    assert "/api/overview?range=1M" in endpoints
    assert "/api/positions/SEC0" in endpoints
    assert "/api/instruments/SEC0/series" in endpoints
    assert "world_em_80_20" in decrypted["rebalance_presets"]

    # Decrypt vault.enc and verify SQLite database integrity
    restored_bytes = crypto.decrypt_bytes((out_dir / "vault.enc").read_bytes(), token)
    restored_db = tmp_path / "restored.db"
    restored_db.write_bytes(restored_bytes)
    rconn = connect(restored_db, read_only=True)
    row = rconn.execute("SELECT import_key FROM instrument WHERE id = 1").fetchone()
    assert row["import_key"] == "SEC0"
    rconn.close()
    assert Decimal("1050") > Decimal(0)
