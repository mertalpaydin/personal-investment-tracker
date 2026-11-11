"""Generate and send the daily portfolio performance email summary.

Invoked at the end of the nightly GitHub Actions workflow (or locally with
--dry-run). Queries data/portfolio.db for the latest valuation and computes:
- Total Portfolio Value
- Daily Gain / Loss (EUR & TWR %)
- Multi-window performance: 1W, 1M, 3M, 6M, 12M, and All Time (TWR % & EUR gain)

Sends a responsive HTML email (with plain-text fallback) via SMTP (e.g. Gmail).
If SMTP credentials are not configured, it logs a notice and exits cleanly without
failing the build.
"""

from __future__ import annotations

import argparse
import os
import re
import smtplib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from portfolio_tracker.api import overview  # noqa: E402
from portfolio_tracker.core import returns  # noqa: E402
from portfolio_tracker.db.connection import DEFAULT_DB_PATH, connect  # noqa: E402
from portfolio_tracker.money import ZERO  # noqa: E402


@dataclass(frozen=True)
class WindowMetric:
    label: str
    from_date: str
    to_date: str
    twr_pct: float | None
    value_eur: float | None


@dataclass(frozen=True)
class HoldingMove:
    name: str
    return_pct: float | None
    gain_eur: float | None
    value_eur: float


@dataclass(frozen=True)
class DailySummary:
    as_of: date
    total_value_eur: float
    one_day_twr_pct: float | None
    one_day_gain_eur: float
    windows: list[WindowMetric]
    dashboard_url: str
    winners: list[HoldingMove]
    losers: list[HoldingMove]


KNOWN_HOLDING_NAMES: dict[str, str] = {
    "ABECG": "Alphabet",
    "AMZN": "Amazon",
    "FTNT.DE": "Fortinet",
    "VRT.DE": "Vertiv",
    "LYBK": "Lyxor Stoxx Banks",
    "EGV2": "Lyxor Overnight Return",
    "UEF5.DE": "UBS MSCI EM SRI",
    "NQSE": "iShares Nasdaq 100",
    "AFS": "Ak Portföy Sağlık",  # noqa: RUF001
    "AFT": "Ak Portföy Teknoloji",
    "TTA": "İş Portföy Altın",  # noqa: RUF001
    "2B70.DE": "iShares US Biotech",
    "SEC0": "iShares Semiconductors",
    "FLXD": "Franklin European Dividend",
    "DRUP.DE": "Amundi Memory Chips",
    "IS0E": "iShares Gold Producers",
    "WMMS.DE": "Amundi MSCI World Value",
    "ISPA.DE": "iShares Global Dividend",
    "SELD.DE": "Amundi Europe Dividend",
    "IS3S-ZB59.DE": "iShares World Value",
}


def short_holding_name(raw_name: str, import_key: str = "") -> str:
    key = import_key.upper().strip()
    if key in KNOWN_HOLDING_NAMES:
        return KNOWN_HOLDING_NAMES[key]
    name = raw_name
    name = re.sub(r"^MULTI-UNITS LUXEMBOURG\s*-\s*", "", name, flags=re.I)
    name = re.sub(
        r"\b(UCITS|ETF|Acc|Dist|dis|Inc\.?|Co\.?|PLC|EUR Hdg A|C-EUR|Fonu|Yabancı Hisse Senedi)\b",  # noqa: RUF001
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s+", " ", name).strip(" -")
    return name or raw_name


def compute_summary(
    conn: sqlite3.Connection,
    person_id: int = 1,
    dashboard_url: str = "",
) -> DailySummary:
    """Extract today's metrics and all window returns from the portfolio database."""
    w_1d = overview.resolve(conn, person_id, "1D")
    closing_val = overview._value_on(conn, person_id, w_1d.end)

    # 1D Performance
    s_1d = returns.summarise(conn, person_id=person_id, start=w_1d.start, end=w_1d.end)
    r_end = conn.execute(
        "SELECT valuation_basis_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
        (person_id, w_1d.end.isoformat()),
    ).fetchone()
    r_start = conn.execute(
        "SELECT valuation_basis_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
        (person_id, w_1d.start.isoformat()),
    ).fetchone()

    v_end = (
        Decimal(str(r_end["valuation_basis_eur"]))
        if r_end and r_end["valuation_basis_eur"]
        else ZERO
    )
    v_start = (
        Decimal(str(r_start["valuation_basis_eur"]))
        if r_start and r_start["valuation_basis_eur"]
        else ZERO
    )
    flow_1d = conn.execute(
        "SELECT sum(net_external_flow_eur) AS f FROM portfolio_daily "
        "WHERE person_id = ? AND date > ? AND date <= ?",
        (person_id, w_1d.start.isoformat(), w_1d.end.isoformat()),
    ).fetchone()
    subsequent_flows_1d = (
        Decimal(str(flow_1d["f"])) if flow_1d and flow_1d["f"] is not None else ZERO
    )
    gain_1d = v_end - v_start - subsequent_flows_1d
    one_day_twr = float(s_1d.twr * 100) if s_1d.twr is not None else None

    # Multi-window: 1W, 1M, 3M, 6M, 12M
    multi_raw = overview._multi_window_returns(conn, person_id)
    windows: list[WindowMetric] = [
        WindowMetric(
            label="Today (1D)",
            from_date=w_1d.start.strftime("%d %b"),
            to_date=w_1d.end.strftime("%d %b"),
            twr_pct=one_day_twr,
            value_eur=float(gain_1d),
        )
    ]

    for m in multi_raw:
        is_year = m["key"] == "12M"
        fmt = "%d %b %Y" if is_year else "%d %b"
        d_from = date.fromisoformat(m["from"]).strftime(fmt)
        d_to = date.fromisoformat(m["to"]).strftime(fmt)
        windows.append(
            WindowMetric(
                label=m["label"],
                from_date=d_from,
                to_date=d_to,
                twr_pct=float(m["twr"] * 100) if m["twr"] is not None else None,
                value_eur=float(m["value_eur"]) if m["value_eur"] is not None else None,
            )
        )

    # All Time
    w_all = overview.resolve(conn, person_id, "ALL")
    s_all = returns.summarise(conn, person_id=person_id, start=w_all.start, end=w_all.end)
    r_start_all = conn.execute(
        "SELECT valuation_basis_eur FROM portfolio_daily WHERE person_id = ? AND date = ?",
        (person_id, w_all.start.isoformat()),
    ).fetchone()
    v_start_all = (
        Decimal(str(r_start_all["valuation_basis_eur"]))
        if r_start_all and r_start_all["valuation_basis_eur"]
        else ZERO
    )
    flow_all = conn.execute(
        "SELECT sum(net_external_flow_eur) AS f FROM portfolio_daily "
        "WHERE person_id = ? AND date > ? AND date <= ?",
        (person_id, w_all.start.isoformat(), w_all.end.isoformat()),
    ).fetchone()
    subsequent_flows_all = (
        Decimal(str(flow_all["f"])) if flow_all and flow_all["f"] is not None else ZERO
    )
    gain_all = v_end - v_start_all - subsequent_flows_all

    windows.append(
        WindowMetric(
            label="All Time",
            from_date=w_all.start.strftime("%d %b %Y"),
            to_date=w_all.end.strftime("%d %b %Y"),
            twr_pct=float(s_all.twr * 100) if s_all.twr is not None else None,
            value_eur=float(gain_all),
        )
    )

    # Daily Movers: Winners & Losers for 1D
    holdings_1d = overview._holdings(conn, person_id, w_1d)
    winners: list[HoldingMove] = []
    losers: list[HoldingMove] = []
    for h in holdings_1d:
        h_name = short_holding_name(h.get("name", ""), h.get("instrument", ""))
        h_ret = float(h["total_return"] * 100) if h.get("total_return") is not None else 0.0
        h_gain = float(h["total_gain_eur"]) if h.get("total_gain_eur") is not None else 0.0
        h_val = float(h.get("value_eur", 0.0))
        perf = HoldingMove(name=h_name, return_pct=h_ret, gain_eur=h_gain, value_eur=h_val)
        if h_gain >= 0:
            winners.append(perf)
        else:
            losers.append(perf)
    winners.sort(key=lambda x: -(x.gain_eur or 0.0))
    losers.sort(key=lambda x: (x.gain_eur or 0.0))

    return DailySummary(
        as_of=w_1d.end,
        total_value_eur=float(closing_val),
        one_day_twr_pct=one_day_twr,
        one_day_gain_eur=float(gain_1d),
        windows=windows,
        dashboard_url=dashboard_url
        or "https://mertalpaydin.github.io/personal-investment-tracker/",
        winners=winners,
        losers=losers,
    )


def _fmt_money(val: float | None, with_sign: bool = True) -> str:
    if val is None:
        return "-"
    if val < 0:
        return f"-EUR {abs(val):,.2f}"
    sign = "+" if with_sign else ""
    return f"{sign}EUR {val:,.2f}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "-"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f}%"


def build_text_email(s: DailySummary) -> str:
    """Plain-text body fallback for mobile notification previews."""
    as_of_str = s.as_of.strftime("%d %b %Y")
    twr_1d_str = _fmt_pct(s.one_day_twr_pct)

    lines = [
        f"Portfolio Daily Summary - {as_of_str}",
        "=" * 50,
        f"Total Value:    EUR {s.total_value_eur:,.2f}",
        f"Today's Move:   {_fmt_money(s.one_day_gain_eur)} ({twr_1d_str})",
        "",
        "Performance Breakdown:",
        "-" * 50,
        f"{'Period':<12} | {'Return':>8} | {'Gain / Loss':>16} | Window",
        "-" * 50,
    ]

    for w in s.windows:
        twr = _fmt_pct(w.twr_pct)
        gain = _fmt_money(w.value_eur)
        date_range = f"{w.from_date} - {w.to_date}" if w.label != "All Time" else "Since Inception"
        lines.append(f"{w.label:<12} | {twr:>8} | {gain:>16} | {date_range}")

    if s.winners:
        lines.extend(
            [
                "",
                f"Daily Winners ({len(s.winners)}):",
                "-" * 50,
                f"{'Holding':<24} | {'Return':>8} | {'Gain / Loss':>14} | Current Value",
                "-" * 50,
            ]
        )
        for h in s.winners:
            twr = _fmt_pct(h.return_pct)
            gain = _fmt_money(h.gain_eur)
            val = f"EUR {h.value_eur:,.2f}"
            lines.append(f"{h.name:<24} | {twr:>8} | {gain:>14} | {val:>13}")

    if s.losers:
        lines.extend(
            [
                "",
                f"Daily Losers ({len(s.losers)}):",
                "-" * 50,
                f"{'Holding':<24} | {'Return':>8} | {'Gain / Loss':>14} | Current Value",
                "-" * 50,
            ]
        )
        for h in s.losers:
            twr = _fmt_pct(h.return_pct)
            gain = _fmt_money(h.gain_eur)
            val = f"EUR {h.value_eur:,.2f}"
            lines.append(f"{h.name:<24} | {twr:>8} | {gain:>14} | {val:>13}")

    lines.extend(
        [
            "",
            "-" * 50,
            f"Open Dashboard: {s.dashboard_url}",
            "=" * 50,
        ]
    )
    return "\n".join(lines)


def _render_movers_html(title: str, movers: list[HoldingMove], is_winners: bool) -> str:
    if not movers:
        return ""
    header_color = "#10b981" if is_winners else "#ef4444"
    header_bg = "#f0fdf4" if is_winners else "#fef2f2"
    border_color = "rgba(16, 185, 129, 0.2)" if is_winners else "rgba(239, 68, 68, 0.2)"

    rows: list[str] = []
    for m in movers:
        is_pos = (m.gain_eur or 0) >= 0
        color = "#10b981" if is_pos else "#ef4444"
        bg = "#f0fdf4" if is_pos else "#fef2f2"
        rows.append(
            f'<tr style="border-bottom: 1px solid #e2e8f0;">'
            f'<td style="padding: 10px 14px; font-weight: 600; color: #0f172a;">{m.name}</td>'
            f'<td style="padding: 10px 14px; text-align: right; font-weight: 700; color: {color};">'
            f'<span style="background-color: {bg}; padding: 3px 8px; border-radius: 4px;">{_fmt_pct(m.return_pct)}</span></td>'
            f'<td style="padding: 10px 14px; text-align: right; font-weight: 600; color: {color}; font-variant-numeric: tabular-nums;">{_fmt_money(m.gain_eur)}</td>'
            f'<td style="padding: 10px 14px; text-align: right; color: #64748b; font-size: 13px;">EUR {m.value_eur:,.2f}</td>'
            f"</tr>"
        )

    tbody = "\n".join(rows)
    return (
        f"<div style='margin-top: 20px;'>"
        f"<div style='margin-bottom: 8px;'>"
        f"<span style='font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: {header_color}; background-color: {header_bg}; border: 1px solid {border_color}; padding: 4px 10px; border-radius: 6px;'>{title}</span>"
        f"</div>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse: collapse; font-size: 13px; border: 1px solid #e2e8f0; border-radius: 8px;'>"
        f"<thead><tr style='background-color: #f8fafc; border-bottom: 2px solid #e2e8f0;'>"
        f"<th align='left' style='padding: 8px 14px; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase;'>Holding</th>"
        f"<th align='right' style='padding: 8px 14px; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase;'>Return</th>"
        f"<th align='right' style='padding: 8px 14px; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase;'>Gain / Loss</th>"
        f"<th align='right' style='padding: 8px 14px; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase;'>Value</th>"
        f"</tr></thead>"
        f"<tbody>{tbody}</tbody></table></div>"
    )


def build_html_email(s: DailySummary) -> str:
    """Modern, responsive HTML email styled to match the tracker theme."""
    as_of_str = s.as_of.strftime("%d %B %Y")
    is_positive_1d = s.one_day_gain_eur >= 0
    move_color = "#10b981" if is_positive_1d else "#ef4444"
    move_bg = "rgba(16, 185, 129, 0.12)" if is_positive_1d else "rgba(239, 68, 68, 0.12)"
    twr_1d_str = _fmt_pct(s.one_day_twr_pct)

    table_rows: list[str] = []
    for w in s.windows:
        is_pos = (w.value_eur or 0) >= 0
        color = "#10b981" if is_pos else "#ef4444"
        bg = "#f0fdf4" if is_pos else "#fef2f2"
        date_range = (
            f"{w.from_date} &ndash; {w.to_date}" if w.label != "All Time" else "Since Inception"
        )
        is_hero_row = w.label in ("Today (1D)", "All Time")
        weight = "700" if is_hero_row else "500"
        row_bg = "#f8fafc" if w.label == "Today (1D)" else "#ffffff"

        table_rows.append(
            f'<tr style="background-color: {row_bg}; border-bottom: 1px solid #e2e8f0;">'
            f'<td style="padding: 12px 14px; font-weight: {weight}; color: #0f172a;">{w.label}</td>'
            f'<td style="padding: 12px 14px; text-align: right; font-weight: 700; color: {color};">'
            f'<span style="background-color: {bg}; padding: 3px 8px; border-radius: 4px;">'
            f"{_fmt_pct(w.twr_pct)}</span></td>"
            f'<td style="padding: 12px 14px; text-align: right; font-weight: 600; color: {color};'
            f' font-variant-numeric: tabular-nums;">{_fmt_money(w.value_eur)}</td>'
            f'<td style="padding: 12px 14px; text-align: right; color: #64748b; font-size: 13px;">'
            f"{date_range}</td></tr>"
        )

    rows_html = "\n".join(table_rows)
    winners_html = _render_movers_html(f"Winners ({len(s.winners)})", s.winners, is_winners=True)
    losers_html = _render_movers_html(f"Losers ({len(s.losers)})", s.losers, is_winners=False)

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        "<title>Portfolio Daily Summary</title></head>"
        '<body style="margin: 0; padding: 24px 12px; background-color: #f1f5f9; '
        "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, "
        'sans-serif; color: #0f172a;">'
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "style='max-width: 620px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; "
        "overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.08); border: 1px solid #e2e8f0;'>"
        "<!-- Header -->"
        "<tr><td style='padding: 24px 28px 16px; background-color: #0f172a; color: #ffffff;'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0'>"
        "<tr><td>"
        "<h1 style='margin: 0; font-size: 20px; font-weight: 700; color: #ffffff;'>"
        "Portfolio Daily Summary</h1>"
        f"<p style='margin: 4px 0 0; font-size: 13px; color: #94a3b8;'>As of {as_of_str}</p>"
        "</td><td align='right'>"
        "<span style='background-color: rgba(255,255,255,0.1); color: #cbd5e1; font-size: 12px; "
        "font-weight: 600; padding: 4px 10px; border-radius: 6px;'>Nightly Update</span>"
        "</td></tr></table></td></tr>"
        "<!-- KPI Cards -->"
        "<tr><td style='padding: 20px 24px 12px;'>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0'><tr>"
        "<td width='48%' style='padding: 16px; background-color: #f8fafc; border-radius: 10px; "
        "border: 1px solid #e2e8f0; vertical-align: top;'>"
        "<span style='font-size: 12px; font-weight: 600; text-transform: uppercase; "
        "letter-spacing: 0.05em; color: #64748b;'>Total Value</span>"
        f"<div style='font-size: 24px; font-weight: 800; color: #0f172a; margin-top: 6px;'>"
        f"EUR {s.total_value_eur:,.2f}</div></td>"
        "<td width='4%'></td>"
        f"<td width='48%' style='padding: 16px; background-color: {move_bg}; border-radius: 10px; "
        f"border: 1px solid {move_color}33; vertical-align: top;'>"
        f"<span style='font-size: 12px; font-weight: 600; text-transform: uppercase; "
        f"letter-spacing: 0.05em; color: {move_color};'>Today (1D)</span>"
        f"<div style='font-size: 22px; font-weight: 800; color: {move_color}; margin-top: 6px;'>"
        f"{_fmt_money(s.one_day_gain_eur)}</div>"
        f"<div style='font-size: 13px; font-weight: 700; color: {move_color}; margin-top: 2px;'>"
        f"{twr_1d_str}</div></td></tr></table></td></tr>"
        "<!-- Table -->"
        "<tr><td style='padding: 12px 24px 20px;'>"
        "<h3 style='margin: 8px 0 12px; font-size: 14px; font-weight: 700; text-transform: uppercase; "
        "letter-spacing: 0.05em; color: #475569;'>Performance Overview</h3>"
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "style='border-collapse: collapse; font-size: 14px; border: 1px solid #e2e8f0; border-radius: 8px;'>"
        "<thead><tr style='background-color: #f8fafc; border-bottom: 2px solid #e2e8f0;'>"
        "<th align='left' style='padding: 10px 14px; font-size: 12px; font-weight: 700; color: #64748b; "
        "text-transform: uppercase;'>Period</th>"
        "<th align='right' style='padding: 10px 14px; font-size: 12px; font-weight: 700; color: #64748b; "
        "text-transform: uppercase;'>Return</th>"
        "<th align='right' style='padding: 10px 14px; font-size: 12px; font-weight: 700; color: #64748b; "
        "text-transform: uppercase;'>Gain / Loss</th>"
        "<th align='right' style='padding: 10px 14px; font-size: 12px; font-weight: 700; color: #64748b; "
        "text-transform: uppercase;'>Window</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        f"{winners_html}"
        f"{losers_html}"
        "</td></tr>"
        "<!-- Footer -->"
        "<tr><td style='padding: 8px 24px 28px; text-align: center;'>"
        f"<a href='{s.dashboard_url}' target='_blank' style='display: inline-block; "
        "background-color: #2563eb; color: #ffffff; text-decoration: none; font-size: 14px; "
        "font-weight: 600; padding: 12px 28px; border-radius: 8px;'>"
        "Open Live Dashboard &rarr;</a>"
        "<p style='margin: 16px 0 0; font-size: 12px; color: #94a3b8;'>"
        "Sent automatically by your Personal Investment Tracker GitHub Action.</p>"
        "</td></tr></table></body></html>"
    )


def send_email(s: DailySummary) -> None:
    """Send summary via SMTP if configured, or print notice if not."""
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "465").strip())
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
    to_addr = (os.environ.get("SMTP_TO") or user).strip()

    if not user or not password:
        print("Notice: SMTP_USER or SMTP_PASSWORD is not set. Skipping email dispatch.")
        print("To receive daily emails, add SMTP_USER and SMTP_PASSWORD to GitHub Secrets.")
        return

    twr_str = _fmt_pct(s.one_day_twr_pct)
    subject = (
        f"Portfolio: EUR {s.total_value_eur:,.2f} "
        f"({_fmt_money(s.one_day_gain_eur)} | {twr_str}) - {s.as_of.strftime('%d %b')}"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(build_text_email(s))
    msg.add_alternative(build_html_email(s), subtype="html")

    print(f"Connecting to SMTP server {host}:{port} as {user}...")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)

    print(f"Successfully sent daily portfolio summary email to {to_addr}!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send daily portfolio summary email.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Path to portfolio.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print email body without sending",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="Dashboard URL",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Error: Database file {args.db} not found.", file=sys.stderr)
        sys.exit(1)

    conn = connect(args.db)
    summary = compute_summary(conn, dashboard_url=args.url)

    if args.dry_run:
        print("\n=== PLAIN TEXT PREVIEW ===")
        print(build_text_email(summary))
        print(f"\n=== HTML PREVIEW (sample length: {len(build_html_email(summary))} bytes) ===")
        return

    send_email(summary)


if __name__ == "__main__":
    main()
