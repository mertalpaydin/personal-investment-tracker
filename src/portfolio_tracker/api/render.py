"""The debug page.

Server-rendered HTML with no build step and no framework. It exists to make the
numbers inspectable, not to be pretty -- so every figure that rests on an
inference says so on the page rather than in a tooltip.

The one piece of design that matters: the currency split is shown per position,
because "your export says +EUR 2,108, the real figure is +EUR 1,088" is the
single most useful thing this tool has to say.
"""

from __future__ import annotations

import html
import sqlite3
from decimal import Decimal

from portfolio_tracker.core import goal, returns
from portfolio_tracker.money import ZERO

_STYLE = """
:root { color-scheme: light dark; --gain:#0a7d3c; --loss:#b3261e; --muted:#6b7280; }
* { box-sizing: border-box; }
body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 2rem 1.5rem; max-width: 68rem; margin-inline: auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; text-transform: uppercase; letter-spacing: .06em;
     color: var(--muted); margin: 2.5rem 0 .75rem; font-weight: 600; }
.sub { color: var(--muted); margin: 0 0 2rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 1rem; }
.card { border: 1px solid color-mix(in srgb, currentColor 15%, transparent);
        border-radius: .5rem; padding: .9rem 1rem; }
.card .k { color: var(--muted); font-size: .8rem; text-transform: uppercase;
           letter-spacing: .04em; }
.card .v { font-size: 1.35rem; font-variant-numeric: tabular-nums; margin-top: .2rem; }
.card.total { border-width: 2px; }
.card.aside { opacity: .75; border-style: dashed; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: .4rem .5rem; border-bottom: 1px solid
         color-mix(in srgb, currentColor 10%, transparent); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 600; font-size: .8rem; text-transform: uppercase; }
.gain { color: var(--gain); } .loss { color: var(--loss); }
.note { color: var(--muted); font-size: .85rem; }
.wrap { overflow-x: auto; }
ul.caveats { padding-left: 1.1rem; color: var(--muted); font-size: .88rem; }
ul.caveats li { margin-bottom: .4rem; }
.tag { font-size: .72rem; padding: .1rem .4rem; border-radius: .25rem;
       border: 1px solid currentColor; opacity: .7; }
.bar { height: .6rem; border-radius: .3rem; margin: 1rem 0 .5rem;
       background: color-mix(in srgb, currentColor 12%, transparent); overflow: hidden; }
.bar span { display: block; height: 100%; background: var(--gain); }
form.goal { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
            margin: .75rem 0 0; font-size: .88rem; color: var(--muted); }
form.goal input { width: 9rem; padding: .3rem .5rem; font: inherit;
                  border-radius: .3rem; background: transparent; color: inherit;
                  border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
form.goal button { padding: .3rem .8rem; font: inherit; border-radius: .3rem;
                   background: transparent; color: inherit; cursor: pointer;
                   border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
"""


def _eur(value: object, *, sign: bool = False) -> str:
    if not isinstance(value, Decimal):
        return "&mdash;"
    formatted = f"{value:,.2f}"
    if sign and value > ZERO:
        formatted = f"+{formatted}"
    return formatted


def _pct(value: object) -> str:
    return f"{value:.2%}" if isinstance(value, Decimal) else "&mdash;"


def _cls(value: object) -> str:
    if not isinstance(value, Decimal) or value == ZERO:
        return ""
    return "gain" if value > ZERO else "loss"


def render_dashboard(conn: sqlite3.Connection, *, person_id: int = 1) -> str:
    latest = conn.execute(
        "SELECT * FROM portfolio_daily WHERE person_id = ? ORDER BY date DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if latest is None:
        raise LookupError("no portfolio history; run `pt rebuild` first")

    summary = returns.summarise(conn, person_id=person_id)
    positions = conn.execute(
        """
        SELECT i.import_key, i.name, i.currency, i.tax_regime, p.*
        FROM position_daily p
        JOIN instrument i ON i.id = p.instrument_id
        JOIN account a ON a.id = p.account_id
        WHERE p.date = ? AND a.person_id = ?
        ORDER BY p.market_value_eur DESC
        """,
        (latest["date"], person_id),
    ).fetchall()

    parts: list[str] = [
        "<style>",
        _STYLE,
        "</style>",
        "<h1>Portfolio</h1>",
        f"<p class='sub'>as of {html.escape(latest['date'])} &middot; "
        f"{summary.days} days tracked</p>",
    ]

    # Headline. Deposits are inside the total; uninvested cash sits beside it,
    # visibly separate, because whether it is still held is unknown.
    parts.append("<div class='cards'>")
    parts.append(_card("Holdings", _eur(latest["market_value_eur"])))
    parts.append(_card("Deposits", _eur(latest["deposits_eur"])))
    parts.append(_card("Total", _eur(latest["total_value_eur"]), extra="total"))
    parts.append("</div>")

    # Outside the total, and split by where it came from. "Uninvested cash:
    # EUR 2,548" answers the wrong question -- capital waiting to be reinvested
    # and income that may already have been spent are not the same thing.
    income_cash = latest["income_cash_eur"]
    capital_cash = latest["cash_eur"] - income_cash
    parts.append("<h2>Outside the total</h2>")
    parts.append("<div class='cards'>")
    parts.append(
        _card(
            "Dividends in cash",
            _eur(income_cash),
            extra="aside",
            note="income received and not reinvested",
        )
    )
    parts.append(
        _card(
            "Capital in cash",
            _eur(capital_cash),
            extra="aside",
            note="sale proceeds awaiting reinvestment",
        )
    )
    parts.append(
        _card(
            "Income received",
            _eur(latest["income_received_eur"]),
            extra="aside",
            note="dividends and interest, ever",
        )
    )
    parts.append("</div>")
    parts.append(
        "<p class='note'>None of these are in the total. The tracker knows what was "
        "received; it cannot know what was then spent, so counting it as held would "
        "be a guess. Cash is attributed <em>capital first, income last</em> &mdash; a "
        "bank account is fungible, so that is a stated convention rather than an "
        "observation.</p>"
    )

    parts.append(_goal_section(conn, person_id))

    parts.append("<h2>Return</h2><div class='cards'>")
    parts.append(_card("Time-weighted", _pct(summary.twr)))
    parts.append(_card("Annualised", _pct(summary.twr_annualised)))
    parts.append(
        _card("Money-weighted", _pct(summary.xirr), note=f"{summary.xirr_reliability} confidence")
    )
    parts.append(_card("Volatility", _pct(summary.volatility)))
    parts.append(_card("Max drawdown", _pct(summary.max_drawdown)))
    parts.append(_card("Contributions", _eur(summary.external_flows_eur)))
    parts.append("</div>")

    parts.append("<h2>Positions</h2>")
    parts.append(
        "<p class='note'>The <em>reported</em> column is what a tracker translating at "
        "today's rate would show. <em>Actual</em> is the real euro figure; the gap is "
        "currency movement.</p>"
    )
    parts.append("<div class='wrap'><table>")
    parts.append(
        "<tr><th>Instrument</th><th>Qty</th><th>Price</th><th>Value (EUR)</th>"
        "<th>Cost</th><th>Reported</th><th>Actual</th><th>Currency</th><th></th></tr>"
    )
    for row in positions:
        tags = []
        if row["valuation_status"] != "OK":
            tags.append(f"<span class='tag'>{html.escape(row['valuation_status'].lower())}</span>")
        if row["tax_regime"] != "DE_INVESTMENT":
            tags.append("<span class='tag'>out of tax scope</span>")
        parts.append(
            "<tr>"
            f"<td><strong>{html.escape(row['import_key'])}</strong> "
            f"<span class='note'>{html.escape(row['name'][:34])}</span></td>"
            f"<td>{_eur(row['quantity'])}</td>"
            f"<td>{row['price_local']:,.4f} {html.escape(row['currency'])}</td>"
            f"<td>{_eur(row['market_value_eur'])}</td>"
            f"<td>{_eur(row['cost_basis_eur'])}</td>"
            f"<td class='{_cls(row['unrealized_local_eur'])}'>"
            f"{_eur(row['unrealized_local_eur'], sign=True)}</td>"
            f"<td class='{_cls(row['unrealized_eur'])}'>"
            f"{_eur(row['unrealized_eur'], sign=True)}</td>"
            f"<td class='{_cls(row['unrealized_fx_eur'])}'>"
            f"{_eur(row['unrealized_fx_eur'], sign=True)}</td>"
            f"<td>{' '.join(tags)}</td>"
            "</tr>"
        )
    parts.append("</table></div>")

    pending = conn.execute(
        "SELECT count(*) AS n FROM pending_entry WHERE status = 'PROPOSED'"
    ).fetchone()["n"]
    if pending:
        parts.append(
            f"<h2>Awaiting review</h2><p class='note'>{pending} inference(s) have not been "
            "confirmed and are not in the ledger. Run <code>pt flows list</code>.</p>"
        )

    if summary.caveats:
        parts.append("<h2>What these numbers rest on</h2><ul class='caveats'>")
        for caveat in summary.caveats:
            parts.append(f"<li>{html.escape(caveat)}</li>")
        parts.append("</ul>")

    parts.append(
        "<p class='note' style='margin-top:2rem'>Planning figures only. The bank's "
        "Steuerbescheinigung is authoritative. Not tax or investment advice.</p>"
    )
    return "\n".join(parts)


def _goal_section(conn: sqlite3.Connection, person_id: int) -> str:
    """The goal panel: a bar, an editable target, and the projection's caveats.

    The target is editable here because a number nobody chose has no business
    driving a progress bar. The form PUTs and reloads -- twelve lines of inline
    JavaScript rather than a framework, for one field.
    """
    try:
        state = goal.progress(conn, person_id=person_id)
    except LookupError:
        return ""

    filled = min(Decimal(1), max(ZERO, state.progress_pct))
    when = state.projected_date.isoformat() if state.projected_date else None
    if when:
        forecast = (
            f"At {_eur(state.monthly_contribution_eur)} EUR/month of contributions and "
            f"no investment return at all, around <strong>{html.escape(when)}</strong>."
        )
    else:
        forecast = "Not enough contribution history to put a date on it."

    caveats = "".join(f"<li>{html.escape(c)}</li>" for c in state.caveats)
    return (
        "<h2>Goal</h2>"
        "<div class='cards'>"
        + _card("Target", _eur(state.target_eur))
        + _card("Reached", _pct(state.progress_pct))
        + _card("Remaining", _eur(state.remaining_eur))
        + "</div>"
        f"<div class='bar'><span style='width:{filled:.4%}'></span></div>"
        f"<p class='note'>{forecast}</p>"
        "<form class='goal' onsubmit=\"event.preventDefault();"
        "fetch('/api/goal',{method:'PUT',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({target_eur:this.target.value})})"
        '.then(r=>r.ok?location.reload():r.json().then(e=>alert(e.detail)))">'
        "<label for='target'>Change target</label>"
        f"<input id='target' name='target' type='number' min='1' step='100' "
        f"value='{state.target_eur:f}'>"
        "<button type='submit'>Save</button>"
        "</form>"
        f"<ul class='caveats'>{caveats}</ul>"
    )


def _card(label: str, value: str, *, extra: str = "", note: str = "") -> str:
    classes = f"card {extra}".strip()
    tail = f"<div class='note'>{html.escape(note)}</div>" if note else ""
    return (
        f"<div class='{classes}'><div class='k'>{html.escape(label)}</div>"
        f"<div class='v'>{value}</div>{tail}</div>"
    )
