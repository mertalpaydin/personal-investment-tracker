"""Build an encrypted, zero-knowledge static site for GitHub Pages.

Pre-computes every read-only API response from the local SQLite database using
FastAPI's in-process ``TestClient``, compresses the bundle with gzip, encrypts
it with AES-256-GCM (see :mod:`portfolio_tracker.core.crypto`), and writes a
self-contained static site into ``dist/``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import warnings
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote as urllib_quote
from urllib.parse import urlencode

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=".*httpx.*")
    from fastapi.testclient import TestClient

from portfolio_tracker import __version__
from portfolio_tracker.api import app as api
from portfolio_tracker.core import crypto
from portfolio_tracker.db.connection import connect

STATIC_SRC = Path(__file__).resolve().parent / "api" / "static"

OVERVIEW_RANGES: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "ALL")
AGG_RANGES: tuple[str, ...] = ("6M", "1Y", "ALL")
ALLOCATION_DIMS: tuple[str, ...] = ("asset_class", "currency", "geography", "sector", "instrument")


def normalize_endpoint_key(path: str, params: dict[str, Any] | None = None) -> str:
    """Canonical ``path?k=v`` key with sorted query parameters.

    Matches the normalization logic in ``static/js/api.js`` so any parameter
    order produces the exact same lookup key.
    """
    if not params:
        return path
    clean: list[tuple[str, str]] = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            clean.append((k, "true" if v else "false"))
        else:
            clean.append((k, str(v)))
    if not clean:
        return path
    return f"{path}?{urlencode(clean)}"


def _fetch_entry(
    client: TestClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform an in-process GET and encode either the JSON body or error state."""
    key = normalize_endpoint_key(path, params)
    response = client.get(key)
    try:
        body = response.json()
    except Exception:
        body = None
    if response.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        return {
            "__error__": True,
            "status": response.status_code,
            "detail": detail if isinstance(detail, str) else f"HTTP {response.status_code}",
            "body": body,
        }
    return {"__ok__": True, "body": body}


def build_snapshot_payload(
    db_path: Path,
    *,
    incoming_dir: Path | None = None,
    include_universe_series: bool = False,
) -> dict[str, Any]:
    """Collect all read-only API endpoints into a single snapshot dictionary."""
    api.configure(db_path=db_path, incoming_dir=incoming_dir)

    endpoints: dict[str, Any] = {}
    rebalance_presets: dict[str, Any] = {}

    with TestClient(api.app) as client:
        base_calls: list[tuple[str, dict[str, Any] | None]] = [
            ("/api/health/data", None),
            ("/api/portfolio", None),
            ("/api/positions", None),
            ("/api/returns", None),
            ("/api/returns/series", None),
            ("/api/brief", None),
            ("/api/goal", None),
            ("/api/savings", None),
            ("/api/savings/inferred", None),
            ("/api/projection", {"paths": 300, "years": 15}),
            ("/api/projection", {"paths": 600, "years": 15}),
            ("/api/projection/sensitivity", {"paths": 300}),
            ("/api/projection/scenarios", {"paths": 300}),
            ("/api/rebalance/presets", None),
            ("/api/pending", None),
            ("/api/incoming", None),
            ("/api/updates", None),
            ("/api/settings/llm", None),
            ("/api/accounts", None),
            ("/api/tax/constants", None),
            ("/api/transactions", {"limit": 200}),
            ("/api/watchlist", None),
            ("/api/watchlist", {"all": True}),
            ("/api/watchlist", {"all": True, "metrics": True}),
            ("/api/watchlist", {"all": True, "metrics": False}),
            ("/api/peers", None),
            ("/api/peers/leaderboard", {"top": 500}),
            ("/api/instruments/categories/suggested", None),
            ("/api/stocks/fundamentals", None),
            ("/api/stocks/fundamentals/status", None),
            ("/api/stocks/score-backtest", {"score": "value"}),
            ("/api/stocks/score-backtest", {"score": "quality"}),
            ("/api/admin/doctor", None),
            ("/api/llm/runs", {"limit": 20}),
        ]

        for r in OVERVIEW_RANGES:
            base_calls.append(("/api/overview", {"range": r}))
        for agg in AGG_RANGES:
            base_calls.append(("/api/returns/aggregated", {"range": agg}))
        for dim in ALLOCATION_DIMS:
            base_calls.append(("/api/allocation", {"dim": dim}))

        current_year = date.today().year
        tax_years = sorted({2024, 2025, 2026, current_year - 1, current_year})
        for y in tax_years:
            base_calls.append(("/api/tax/allowance", {"year": y}))
            base_calls.append(("/api/tax/vorabpauschale", {"year": y}))
            base_calls.append(("/api/scenario/harvest", {"year": y}))
            base_calls.append(("/api/tax/explain", {"year": y}))

        for path, params in base_calls:
            key = normalize_endpoint_key(path, params)
            endpoints[key] = _fetch_entry(client, path, params)

        # Collect per-position detail pages for all open and closed holdings
        position_keys: set[str] = set()
        pos_entry = endpoints.get("/api/positions", {})
        if pos_entry.get("__ok__") and isinstance(pos_entry.get("body"), dict):
            for p in pos_entry["body"].get("positions") or []:
                if p.get("instrument"):
                    position_keys.add(str(p["instrument"]))

        over_all = endpoints.get("/api/overview?range=ALL", {})
        if over_all.get("__ok__") and isinstance(over_all.get("body"), dict):
            for h in over_all["body"].get("holdings") or []:
                if h.get("instrument"):
                    position_keys.add(str(h["instrument"]))
            closed = over_all["body"].get("closed_trades")
            if isinstance(closed, dict):
                for t in closed.get("trades") or []:
                    if t.get("instrument"):
                        position_keys.add(str(t["instrument"]))

        for inst_key in sorted(position_keys):
            encoded = urllib_quote(inst_key, safe="")
            pos_path = f"/api/positions/{encoded}"
            endpoints[pos_path] = _fetch_entry(client, pos_path)

        # Collect series and peer comparisons for owned & watched instruments
        series_keys: set[str] = set(position_keys)
        watch_entry = endpoints.get("/api/watchlist?all=true&metrics=false", {})
        if watch_entry.get("__ok__") and isinstance(watch_entry.get("body"), dict):
            for item in watch_entry["body"].get("watchlist") or []:
                status = item.get("status")
                inst = item.get("instrument")
                if inst and (include_universe_series or status in ("OWNED", "WATCH")):
                    series_keys.add(str(inst))

        for inst_key in sorted(series_keys):
            encoded = urllib_quote(inst_key, safe="")
            series_path = f"/api/instruments/{encoded}/series"
            endpoints[series_path] = _fetch_entry(client, series_path)
            for mode in ("value", "momentum"):
                peer_path = f"/api/peers/{encoded}"
                peer_params = {"mode": mode, "owned": False, "top": 5}
                p_key = normalize_endpoint_key(peer_path, peer_params)
                endpoints[p_key] = _fetch_entry(client, peer_path, peer_params)

        # Precompute rebalance calculator responses for each preset
        presets_entry = endpoints.get("/api/rebalance/presets", {})
        if presets_entry.get("__ok__") and isinstance(presets_entry.get("body"), dict):
            for preset in presets_entry["body"].get("presets") or []:
                pid = preset.get("id")
                if not pid:
                    continue
                calc_resp = client.post(
                    "/api/rebalance/calculate",
                    json={
                        "dimension": preset.get("dimension", "risk_bucket"),
                        "targets": preset.get("targets", {}),
                        "cash_inflow_eur": 1000,
                    },
                )
                if calc_resp.status_code == 200:
                    rebalance_presets[str(pid)] = calc_resp.json()

    health_body = endpoints.get("/api/health/data", {}).get("body") or {}
    as_of = health_body.get("as_of") or date.today().isoformat()

    return {
        "meta": {
            "as_of": as_of,
            "priced_to": health_body.get("priced_to") or as_of,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "version": __version__,
        },
        "endpoints": endpoints,
        "rebalance_presets": rebalance_presets,
    }


def _rewrite_index_for_static(html_text: str) -> str:
    """Convert root-relative ``/static/...`` URLs to ``./static/...`` for GitHub Pages.

    GitHub Pages project sites are served under ``/<repo-name>/``, where a
    leading slash would point at the domain root and 404. Relative paths work
    both at the domain root and under any subpath. Also relaxes the CSP header
    absent on static hosting while keeping self-only asset loading.
    """
    rewritten = html_text.replace('href="/static/', 'href="./static/').replace(
        'src="/static/', 'src="./static/'
    )
    manifest_tag = (
        '<link rel="manifest" href="./manifest.json">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
        '<meta name="mobile-web-app-capable" content="yes">\n'
    )
    if 'rel="manifest"' not in rewritten:
        rewritten = rewritten.replace("</head>", f"{manifest_tag}</head>", 1)
    return rewritten


def _web_manifest() -> dict[str, Any]:
    return {
        "name": "Portfolio Tracker",
        "short_name": "Portfolio",
        "description": "Personal investment tracker (encrypted static snapshot)",
        "start_url": "./",
        "display": "standalone",
        "background_color": "#14161a",
        "theme_color": "#14161a",
        "icons": [
            {
                "src": (
                    "data:image/svg+xml,"
                    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
                    "<rect width='100' height='100' rx='20' fill='%2314161a'/>"
                    "<text x='50' y='68' font-size='55' text-anchor='middle'>&#128200;</text>"
                    "</svg>"
                ),
                "sizes": "192x192",
                "type": "image/svg+xml",
            }
        ],
    }


def build_static_site(
    db_path: Path,
    out_dir: Path,
    token: str,
    *,
    include_encrypted_db: bool = True,
    iterations: int = crypto.PBKDF2_ITERATIONS,
) -> Path:
    """Build the complete static site with an AES-256-GCM encrypted snapshot."""
    cleaned_token = token.strip()
    if len(cleaned_token) < 6:
        raise ValueError("Access token must be at least 6 characters long.")

    payload = build_snapshot_payload(db_path)
    envelope = crypto.encrypt_snapshot(payload, cleaned_token, iterations=iterations)

    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    static_dest = out_dir / "static"
    shutil.copytree(
        STATIC_SRC,
        static_dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    index_src = (STATIC_SRC / "index.html").read_text(encoding="utf-8")
    (out_dir / "index.html").write_text(_rewrite_index_for_static(index_src), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(_web_manifest(), indent=2),
        encoding="utf-8",
    )

    snapshot_path = static_dest / "snapshot.enc.json"
    snapshot_path.write_text(
        json.dumps(envelope, separators=(",", ":")),
        encoding="utf-8",
    )

    if include_encrypted_db and db_path.exists():
        # Export a consistent, single-file SQLite backup (merging any WAL frames)
        # and encrypt it with AES-256-GCM so the scheduled GitHub Action can
        # refresh daily prices in CI without storing plaintext data on GitHub.
        with tempfile.TemporaryDirectory() as tmp_dir:
            clean_copy = Path(tmp_dir) / "clean.sqlite"
            with (
                closing(connect(db_path, read_only=True)) as src_conn,
                closing(connect(clean_copy)) as dst_conn,
            ):
                src_conn.backup(dst_conn)
            db_bytes = clean_copy.read_bytes()
        vault_bytes = crypto.encrypt_bytes(db_bytes, cleaned_token, iterations=iterations)
        (out_dir / "vault.enc").write_bytes(vault_bytes)

    return snapshot_path
