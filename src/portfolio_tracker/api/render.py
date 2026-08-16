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
from pathlib import Path

from portfolio_tracker.core import (
    attribution,
    goal,
    inbox,
    incoming,
    look_through,
    projection,
    returns,
    vorabpauschale,
)
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
form.goal button, .actions button { padding: .3rem .8rem; font: inherit;
                   border-radius: .3rem; background: transparent; color: inherit;
                   cursor: pointer;
                   border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
.actions button[disabled] { opacity: .4; cursor: not-allowed; }
.actions button.danger { color: var(--loss); }
.proposal { border: 1px solid color-mix(in srgb, currentColor 15%, transparent);
            border-radius: .5rem; padding: .75rem 1rem; margin-bottom: .6rem; }
.proposal .head { display: flex; gap: .5rem; align-items: center; }
.proposal .basis { margin: .4rem 0 .6rem; font-size: .9rem; }
.actions { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
.actions label { color: var(--muted); font-size: .85rem; }
.actions input { width: 8rem; padding: .3rem .5rem; font: inherit; border-radius: .3rem;
                 background: transparent; color: inherit; text-align: right;
                 border: 1px solid color-mix(in srgb, currentColor 30%, transparent); }
.note.warn { color: var(--loss); }
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
    # Earned and not yet credited. Real money the balance does not show, and
    # nothing else in this system reports it between payment dates.
    unpaid = conn.execute(
        "SELECT total(a.accrued_cum_eur) AS eur FROM interest_accrual a "
        "JOIN account c ON c.id = a.account_id "
        "WHERE c.person_id = ? AND a.date = (SELECT max(b.date) FROM interest_accrual b "
        "                                    WHERE b.account_id = a.account_id)",
        (person_id,),
    ).fetchone()["eur"]
    if unpaid:
        parts.append(
            _card(
                "Interest accrued",
                _eur(Decimal(str(unpaid))),
                extra="aside",
                note="earned, not yet credited",
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
    parts.append(_projection_section(conn, person_id))

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

    parts.append(_incoming_section(conn))

    parts.append(_attribution_section(conn, person_id))

    parts.append(_exposure_section(conn, person_id))

    parts.append(_scenario_section(conn, person_id))

    parts.append(_vorabpauschale_section(conn, person_id))

    parts.append(_review_section(conn))

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


_REVIEW_SCRIPT = """
function ptPost(url, body) {
  return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                     body: JSON.stringify(body || {})})
    .then(r => r.json().then(j => r.ok ? j : Promise.reject(j.detail || r.statusText)));
}
function ptDecide(id, action) {
  const row = document.getElementById('p' + id);
  const amount = row.querySelector('input[name=amount]');
  if (action === 'reject') {
    const reason = prompt('Why did this not happen? A rejection is a claim about reality.');
    if (!reason) return;
    ptPost('/api/pending/' + id + '/reject', {reason: reason})
      .then(() => location.reload()).catch(e => alert(e));
    return;
  }
  const edited = amount.value !== amount.defaultValue ? {amount_local: amount.value} : {};
  ptPost('/api/pending/' + id + '/confirm', edited)
    .then(() => location.reload()).catch(e => alert(e));
}
function ptRebuild(button) {
  button.disabled = true; button.textContent = 'Rebuilding...';
  ptPost('/api/admin/rebuild', {})
    .then(r => { if (r.breaks && r.breaks.length) { alert(r.breaks.join('\\n')); }
                 location.reload(); })
    .catch(e => { alert(e); button.disabled = false; button.textContent = 'Rebuild now'; });
}
"""


def _review_section(conn: sqlite3.Connection) -> str:
    """Proposals awaiting a decision, each with the arithmetic behind it.

    The amount is editable and the basis is printed in full beside the button.
    That is the whole reason this is safe to expose: a confirmation is a real,
    irreversible write, so what makes it a decision rather than a click is
    having the guess and its derivation in front of you when you make it.
    """
    entries = inbox.listing(conn)
    stale = inbox.stale_after_decision(conn)
    if not entries and not stale:
        return ""

    parts = [f"<script>{_REVIEW_SCRIPT}</script>", "<h2>Awaiting review</h2>"]

    if stale:
        parts.append(
            "<p class='note warn'>A decision has been made since the figures above were "
            "computed, so they are out of date. "
            "<button onclick='ptRebuild(this)'>Rebuild now</button></p>"
        )

    if not entries:
        return "\n".join(parts)

    parts.append(
        f"<p class='note'>{len(entries)} inference(s) are <strong>not</strong> in the ledger "
        "and affect nothing above. Read the basis before confirming: the amount is a "
        "guess about money the source never recorded, and you are usually the better "
        "authority. Correcting it here keeps both figures.</p>"
    )
    for entry in entries:
        disabled = "" if entry.bookable else " disabled title='no booking rule for this kind yet'"
        parts.append(
            f"<div class='proposal' id='p{entry.id}'>"
            f"<div class='head'><span class='tag'>{html.escape(entry.kind)}</span> "
            f"<strong>{html.escape(entry.proposed_date.isoformat())}</strong> "
            f"<span class='note'>#{entry.id}</span></div>"
            f"<p class='basis'>{html.escape(entry.basis)}</p>"
            "<div class='actions'>"
            f"<label>{html.escape(entry.currency)}</label>"
            f"<input name='amount' type='number' step='0.01' min='0.01' "
            f"value='{entry.amount_local:f}'>"
            f"<button onclick='ptDecide({entry.id}, \"confirm\")'{disabled}>Confirm</button>"
            f"<button class='danger' onclick='ptDecide({entry.id}, \"reject\")'>Reject</button>"
            "</div></div>"
        )
    return "\n".join(parts)


_INCOMING_SCRIPT = """
function ptImport(name, button) {
  const row = document.getElementById('f' + button.dataset.row);
  const fund = row.querySelector('select[name=fund]').value;
  const when = row.querySelector('input[name=asof]').value;
  if (!fund) { alert('Choose which fund this file belongs to.'); return; }
  if (!when) { alert('Give the date the fund published it.'); return; }
  button.disabled = true; button.textContent = 'Importing...';
  ptPost('/api/incoming/import', {name: name, instrument: fund, as_of: when})
    .then(() => location.reload())
    .catch(e => { alert(e); button.disabled = false; button.textContent = 'Import'; });
}
function ptProposeVap(button) {
  const year = document.getElementById('vapyear').value;
  button.disabled = true; button.textContent = 'Computing...';
  ptPost('/api/vorabpauschale/propose?year=' + encodeURIComponent(year), {})
    .then(r => { alert(r.queued + ' charge(s) queued for review, EUR ' +
                       (r.total_taxable_eur || 0).toFixed(2) + ' taxable.');
                 location.reload(); })
    .catch(e => { alert(e); button.disabled = false; button.textContent = 'Compute'; });
}
"""


def _incoming_section(conn: sqlite3.Connection) -> str:
    """The drop folder: what is in it, and what the tool thinks each file is.

    The detection is a proposal and the page makes that obvious -- the fund and
    the publication date are empty controls the owner fills in. A holdings file
    does not say which fund it belongs to, and a wrong guess would attach one
    issuer's constituents to another fund and corrupt every exposure figure
    without erroring anywhere.
    """
    found = incoming.scan(_incoming_dir())
    funds = conn.execute(
        "SELECT import_key, name FROM instrument WHERE is_fund = 1 ORDER BY import_key"
    ).fetchall()

    parts = [
        f"<script>{_INCOMING_SCRIPT}</script>",
        "<h2>Drop folder</h2>",
        f"<p class='note'>Put issuer holdings files in <code>{html.escape(str(_incoming_dir()))}"
        "</code> and they appear here. Nothing is imported until you say which fund a "
        "file is for &mdash; the file itself does not reliably say, and attaching one "
        "fund's constituents to another would corrupt every exposure figure silently.</p>",
    ]
    if not found:
        parts.append("<p class='note'>Nothing waiting.</p>")
        return "\n".join(parts)

    options = "".join(
        f"<option value='{html.escape(f['import_key'])}'>{html.escape(f['import_key'])}"
        f" &mdash; {html.escape(f['name'][:34])}</option>"
        for f in funds
    )

    parts.append("<div class='wrap'><table>")
    parts.append("<tr><th>File</th><th>What it looks like</th><th>Fund</th><th>Published</th>"
                 "<th></th></tr>")  # fmt: skip
    for index, entry in enumerate(found):
        if entry.importable:
            when = entry.suggested_as_of.isoformat() if entry.suggested_as_of else ""
            control = f"<select name='fund'><option value=''>choose…</option>{options}</select>"
            date_input = f"<input name='asof' type='date' value='{when}'>"
            button = (
                f"<button data-row='{index}' "
                f'onclick="ptImport({entry.name!r}, this)">Import</button>'
            )
            detail = html.escape(entry.summary)
        else:
            control = date_input = "<span class='note'>&mdash;</span>"
            button = ""
            detail = html.escape(entry.problem or entry.summary or "not recognised")
            if entry.problem:
                detail = f"<span class='note warn'>{detail}</span>"

        parts.append(
            f"<tr id='f{index}'>"
            f"<td><strong>{html.escape(entry.name)}</strong> "
            f"<span class='note'>{entry.size_bytes:,} bytes</span></td>"
            f"<td class='note'>{detail}</td>"
            f"<td>{control}</td><td>{date_input}</td><td class='actions'>{button}</td>"
            "</tr>"
        )
    parts.append("</table></div>")
    parts.append(
        "<p class='note'>Imported files move to <code>imported/</code> rather than being "
        "deleted, so the folder shows what is still waiting and nothing you supplied is "
        "destroyed.</p>"
    )
    return "\n".join(parts)


def _vorabpauschale_section(conn: sqlite3.Connection, person_id: int) -> str:
    """The January charge: what is stored, and a button to compute it.

    Computing only *proposes*. The charges land in the review inbox above,
    where confirming is a deliberate act -- which matters more for this kind
    than any other, since it is the one figure that ends up on an *Anlage KAP*
    and it rests on a rate published once a year plus a Teilfreistellung
    classification the tool cannot verify.
    """
    stored = conn.execute(
        "SELECT v.tax_year, v.status, i.import_key, v.taxable_eur, v.basisertrag_eur "
        "FROM vorabpauschale v JOIN instrument i ON i.id = v.instrument_id "
        "ORDER BY v.tax_year DESC, i.import_key"
    ).fetchall()
    latest = conn.execute("SELECT max(date) AS d FROM portfolio_daily").fetchone()["d"]
    default_year = (int(latest[:4]) - 1) if latest else 2025

    booked = vorabpauschale.taxable_income(conn, tax_year=default_year + 1, person_id=person_id)

    parts = [
        "<h2>Vorabpauschale</h2>",
        "<p class='note'>German investors owe tax each January on a <em>deemed</em> "
        "distribution from every accumulating fund that rose. No money arrives; the bank "
        "withholds anyway. Confirming a charge also raises the cost basis, so the same "
        "euros are not taxed again when you sell.</p>",
        "<form class='goal' onsubmit='event.preventDefault()'>"
        "<label for='vapyear'>Charge for year</label>"
        f"<input id='vapyear' type='number' min='2018' max='2100' value='{default_year}'>"
        "<button onclick='ptProposeVap(this)'>Compute</button>"
        "</form>",
    ]

    if stored:
        parts.append("<div class='wrap'><table>")
        parts.append(
            "<tr><th>Year</th><th>Fund</th><th>Basisertrag</th><th>Taxable</th><th>Status</th></tr>"
        )
        for row in stored:
            state = (
                "<span class='tag'>booked</span>"
                if row["status"] == "ACTUAL"
                else "<span class='tag'>not in the ledger</span>"
            )
            parts.append(
                f"<tr><td>{row['tax_year']}</td>"
                f"<td><strong>{html.escape(row['import_key'])}</strong></td>"
                f"<td>{_eur(row['basisertrag_eur'])}</td>"
                f"<td>{_eur(row['taxable_eur'])}</td><td>{state}</td></tr>"
            )
        parts.append("</table></div>")

    if booked:
        total = sum((amount for _, amount in booked), ZERO)
        parts.append(
            f"<p class='note'>{_eur(total)} EUR of it is booked as {default_year + 1} income "
            "and is inside the tax figures. Tax is due on it although no money arrived.</p>"
        )
    elif stored:
        parts.append(
            "<p class='note warn'>Nothing is booked yet, so none of this is in the tax "
            "figures. Confirm the proposals under <em>Awaiting review</em>.</p>"
        )
    return "\n".join(parts)


_SCENARIO_SCRIPT = """
function ptPriceSale() {
  const fund = document.getElementById('selfund').value;
  const qty = document.getElementById('selqty').value;
  const out = document.getElementById('selout');
  if (!fund) { out.innerHTML = ''; return; }
  let url = '/api/scenario/sell?instrument=' + encodeURIComponent(fund);
  if (qty) url += '&qty=' + encodeURIComponent(qty);
  out.innerHTML = "<p class='note'>pricing...</p>";
  fetch(url).then(r => r.json().then(j => r.ok ? j : Promise.reject(j.detail)))
    .then(s => {
      const rows = s.fifo_consumes.map(l =>
        `<tr><td>${l.opened}</td><td>${l.quantity.toLocaleString()}</td>` +
        `<td>${l.cost_basis_eur.toFixed(2)}</td><td>${l.proceeds_eur.toFixed(2)}</td>` +
        `<td>${l.gross_gain_eur.toFixed(2)}</td></tr>`).join('');
      out.innerHTML =
        `<div class='cards'>
           <div class='card'><div class='k'>Proceeds</div>
             <div class='v'>${s.proceeds_eur.toFixed(2)}</div></div>
           <div class='card'><div class='k'>Taxable gain</div>
             <div class='v'>${s.taxable_gain_eur.toFixed(2)}</div></div>
           <div class='card total'><div class='k'>Tax this sale adds</div>
             <div class='v'>${s.marginal_tax_eur.toFixed(2)}</div>
             <div class='note'>allowance left after: ${
               s.allowance.remaining_after_eur.toFixed(2)}</div></div>
         </div>
         <p class='note'>FIFO would consume these lots &mdash; selling part of a holding
          realises the <em>oldest</em> units, which usually carry a different gain.</p>
         <div class='wrap'><table><tr><th>Opened</th><th>Units</th><th>Cost</th>
           <th>Proceeds</th><th>Gain</th></tr>${rows}</table></div>
         <ul class='caveats'>${s.caveats.map(c => '<li>' + c + '</li>').join('')}</ul>`;
    })
    .catch(e => { out.innerHTML = "<p class='note warn'>" + e + "</p>"; });
}
"""


def _scenario_section(conn: sqlite3.Connection, person_id: int) -> str:
    """Price a sale before making it. Reads only; writes nothing, and says so."""
    holdings = conn.execute(
        "SELECT DISTINCT i.import_key, i.name FROM lot l "
        "JOIN instrument i ON i.id = l.instrument_id "
        "JOIN account a ON a.id = l.account_id "
        "WHERE l.qty_remaining > 0 AND a.person_id = ? ORDER BY i.import_key",
        (person_id,),
    ).fetchall()
    if not holdings:
        return ""

    options = "".join(
        f"<option value='{html.escape(h['import_key'])}'>{html.escape(h['import_key'])}"
        f" &mdash; {html.escape(h['name'][:34])}</option>"
        for h in holdings
    )
    return (
        f"<script>{_SCENARIO_SCRIPT}</script>"
        "<h2>What would selling cost?</h2>"
        "<p class='note'>Priced at the last observed close. Nothing is written, and no "
        "future return is assumed anywhere in the figure. The number that matters is what "
        "the sale <em>adds</em> to this year's tax: the same gain costs nothing or 26.375% "
        "depending only on what your allowance has already absorbed.</p>"
        "<form class='goal' onsubmit='event.preventDefault();ptPriceSale()'>"
        f"<select id='selfund'><option value=''>choose a holding…</option>{options}</select>"
        "<label for='selqty'>units (blank = all)</label>"
        "<input id='selqty' type='number' step='any' min='0'>"
        "<button type='submit'>Price it</button>"
        "</form>"
        "<div id='selout'></div>"
    )


def _projection_section(conn: sqlite3.Connection, person_id: int) -> str:
    """When the target is reached, across stated returns and the holdings' own history."""
    try:
        result = projection.project(conn, person_id=person_id)
    except (LookupError, projection.ProjectionError):
        return ""
    if result.already_reached:
        return ""

    rows = "".join(
        f"<tr><td>{r.annual_return:.0%} a year</td>"
        f"<td>{
            html.escape(r.reached_on.isoformat())
            if r.reached_on
            else 'not within ' + str(result.horizon_years) + ' years'
        }</td></tr>"
        for r in result.scenarios
    )
    parts = [
        "<h2>When you reach the target</h2>",
        "<p class='note'>These are <strong>assumptions, not predictions</strong>. The tool "
        "does not choose between them &mdash; the spread between the rows is the answer, "
        "because it shows how much the date rests on the assumption.</p>",
        "<div class='wrap'><table>",
        "<tr><th>If the portfolio returns…</th><th>Target reached</th></tr>",
        rows,
        "</table></div>",
        # The contribution figure is a measurement of the owner's own behaviour
        # and is the input they are most likely to want to change.
        f"<p class='note'>{html.escape(result.contributions.sentence)}</p>",
        "<form class='goal' onsubmit=\"event.preventDefault();"
        "location.search='?monthly='+this.monthly.value\">"
        "<label for='monthly'>Assume I save</label>"
        f"<input id='monthly' name='monthly' type='number' step='50' min='0' "
        f"value='{result.contributions.monthly_eur:f}'>"
        "<button type='submit'>EUR a month</button>"
        "</form>",
    ]

    if result.bootstrap is not None:
        b = result.bootstrap
        parts.append(
            "<p class='note'>Resampling the price history of what you hold today "
            f"({b.model.days} days, {b.block_days}-day blocks): middle path "
            f"<strong>{b.median}</strong>, "
            f"between {b.earliest_decile} and {b.latest_decile}.</p>"
        )
        parts.append("<ul class='caveats'>")
        parts += [f"<li>{html.escape(c)}</li>" for c in b.caveats]
        parts.append("</ul>")
    return "\n".join(parts)


def _incoming_dir() -> Path:
    """Read from the app module so `run.py` and the tests can point it elsewhere."""
    from portfolio_tracker.api import app as api

    return Path(api.INCOMING_DIR)


def _attribution_section(conn: sqlite3.Connection, person_id: int) -> str:
    """Where the return came from, with the parts adding to the whole.

    The portfolio-level version of the sentence this project exists for: the
    holdings can be up 82% and the euro return be 56%, and the difference has a
    name and a size. Per position that has always been visible; across the
    portfolio it was not computable at all until the daily contributions were
    linked properly.
    """
    state = attribution.summarise(conn, person_id=person_id)
    if state is None:
        return ""

    rows = [
        ("Asset prices", state.local, "the holdings, in the currency each is quoted in"),
        ("Currency", state.currency, "the opening capital, re-translated as rates moved"),
        ("Income", state.income, "distributions and credited interest"),
        ("Trading", state.unexplained, "fills away from the close, fees, intraday changes"),
    ]
    body = "".join(
        f"<tr><td>{html.escape(label)} <span class='note'>{html.escape(note)}</span></td>"
        f"<td class='{_cls(value)}'>{_pct(value)}</td></tr>"
        for label, value, note in rows
    )
    return (
        "<h2>Where the return came from</h2>"
        "<div class='wrap'><table>"
        "<tr><th>Source</th><th>Contribution</th></tr>"
        f"{body}"
        f"<tr><th>Total return</th><th class='{_cls(state.total)}'>{_pct(state.total)}</th></tr>"
        "</table></div>"
        "<p class='note'>Daily contributions are Cari&ntilde;o-smoothed onto the compounded "
        "return, so these four add to the total <em>exactly</em> &mdash; adding them up "
        "unsmoothed leaves a gap of several percentage points, which is why the residual "
        "is checked rather than assumed. Trading is named rather than folded into the "
        "others: it is real return that no price or rate explains.</p>"
    )


def _exposure_section(conn: sqlite3.Connection, person_id: int) -> str:
    """Currency exposure both ways, and the largest underlying issuers.

    Two columns, side by side, is the only presentation that makes the point:
    the *quoted* column is what a listing-venue breakdown says, the
    *look-through* column is what the portfolio is actually exposed to, and the
    gap between them is a risk that was invisible until the compositions were
    imported. Showing only the corrected figure would hide the correction.

    The coverage line is not decoration either. A 50%-covered breakdown and a
    fully covered one render identically without it.
    """
    try:
        exposure = look_through.exposure(conn, person_id=person_id, dimension="currency")
    except look_through.NoValuation:
        return ""
    if not exposure.coverage.by_basis.get(look_through.LOOK_THROUGH):
        # Nothing has been looked through, so the "corrected" column would
        # simply restate the quoted one. Point at the command instead.
        return (
            "<h2>Exposure</h2><p class='note'>No fund compositions have been imported, so "
            "currency exposure is still the quote currency: a euro-quoted global tracker "
            "reads as euro risk. Load a constituent list with "
            "<code>pt look-through import</code>.</p>"
        )

    quoted: dict[str, Decimal] = {}
    for row in conn.execute(
        "SELECT i.currency AS bucket, p.market_value_eur FROM position_daily p "
        "JOIN instrument i ON i.id = p.instrument_id "
        "JOIN account a ON a.id = p.account_id WHERE p.date = ? AND a.person_id = ?",
        (exposure.as_of.isoformat(), person_id),
    ):
        quoted[row["bucket"]] = quoted.get(row["bucket"], ZERO) + row["market_value_eur"]

    parts = [
        "<h2>Exposure</h2>",
        "<p class='note'>The <em>quoted</em> column is the currency each instrument is "
        "listed in. <em>Look-through</em> is what its constituents are actually "
        "denominated in. A euro-quoted global tracker is a dollar asset with a euro "
        "ticker, and only the second column knows that.</p>",
        "<div class='wrap'><table>",
        "<tr><th>Currency</th><th>Look-through</th><th>Weight</th><th>Quoted</th>"
        "<th>Difference</th></tr>",
    ]
    for bucket in exposure.buckets:
        was = quoted.get(bucket.name, ZERO)
        delta = bucket.value_eur - was
        parts.append(
            "<tr>"
            f"<td><strong>{html.escape(bucket.name)}</strong></td>"
            f"<td>{_eur(bucket.value_eur)}</td>"
            f"<td>{_pct(bucket.weight)}</td>"
            f"<td>{_eur(was)}</td>"
            f"<td class='{_cls(delta)}'>{_eur(delta, sign=True)}</td>"
            "</tr>"
        )
    parts.append("</table></div>")

    covered = exposure.coverage.look_through_pct
    parts.append(
        f"<p class='note{'' if covered > Decimal('0.9') else ' warn'}'>"
        f"{covered:.1%} of holdings attributed on evidence &mdash; a published "
        "constituent list, or a holding that is its own exposure.</p>"
    )

    top = look_through.concentration(conn, person_id=person_id, top=10)
    if top.underlyings:
        parts.append(
            "<p class='note'>Largest underlying issuers, aggregated across every fund "
            "<em>and</em> direct holding. Overlap between funds is invisible at the "
            "instrument level and is usually where the real concentration is.</p>"
        )
        parts.append("<div class='wrap'><table>")
        parts.append("<tr><th>Issuer</th><th>Value (EUR)</th><th>Weight</th><th>Held via</th></tr>")
        for u in top.underlyings:
            via = ", ".join(f"{html.escape(key)} {value:,.0f}" for key, value in u.via)
            parts.append(
                "<tr>"
                f"<td><strong>{html.escape(u.name)}</strong> "
                f"<span class='note'>{html.escape(u.identifier or '')}</span></td>"
                f"<td>{_eur(u.value_eur)}</td>"
                f"<td>{_pct(u.weight)}</td>"
                f"<td class='note'>{via}</td>"
                "</tr>"
            )
        parts.append("</table></div>")

    if exposure.caveats:
        parts.append("<ul class='caveats'>")
        parts += [f"<li>{html.escape(c)}</li>" for c in exposure.caveats]
        parts.append("</ul>")
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
