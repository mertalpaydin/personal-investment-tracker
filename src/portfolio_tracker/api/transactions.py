"""Recording a trade from the page, and saying what it will do first.

This is the surface that was missing. Until now a trade could only arrive as a
broker CSV export: there was no ``POST /api/transactions``, and no CLI command
to enter a single trade either. The owner of this tool does not use a terminal,
so in practice a trade made on Tuesday could not be recorded until the broker
published an export containing it.

``POST /api/transactions/validate`` is the important endpoint here, not
``POST /api/transactions``. Before a sale is saved it says which tax lots FIFO
will consume, at what cost, what the taxable gain is after advance-charge
relief and partial exemption, and how much tax-free allowance is left
afterwards. That is the moment this tool is worth having, and it is the thing
the command line used to provide by making you read a report before typing a
command.

Three rules the implementation follows:

**Validate never writes and never refuses to answer.** It returns 200 even for
a trade it considers invalid, with the objections in the body. A form that
returns 422 on every keystroke cannot validate as you type.

**Validate and save share one code path.** The preview comes from
``core.scenario.sell()`` -- the same function that prices the read-only
scenario endpoint and drives the harvest planner -- rather than from a second
implementation of the tax chain. Two implementations is how a preview comes to
disagree with the thing it previews.

**A refusal carries its remedy.** Every error has a stable ``code`` and, where
one exists, a ``remedy`` naming the endpoint that would fix it, so the page can
render a button instead of a dead end.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from portfolio_tracker.api.deps import get_db, get_writable_db
from portfolio_tracker.core import ledger, rebuild, scenario, tax
from portfolio_tracker.db.connection import transaction
from portfolio_tracker.money import ZERO, plain, quantize_money

router = APIRouter(tags=["transactions"])

#: Types the page may record. `VORABPAUSCHALE` is deliberately absent: an
#: advance tax charge is computed from a published rate and belongs in the
#: review inbox, where its derivation can be read before it is booked.
EnterableType = Literal[
    "BUY",
    "SELL",
    "DIVIDEND",
    "INTEREST",
    "FEE",
    "TAX_WITHHELD",
    "DEPOSIT",
    "WITHDRAWAL",
    "TRANSFER_IN",
    "TRANSFER_OUT",
]

#: Re-exported from `core.ledger`, which owns it. The mapping decides whether
#: a row issues units in the return calculation, so there must be exactly one.
FLOW_CLASS = ledger.FLOW_CLASS

NEEDS_INSTRUMENT = {"BUY", "SELL", "DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"}
FORBIDS_INSTRUMENT = {"DEPOSIT", "WITHDRAWAL", "INTEREST"}
NEEDS_QUANTITY = {"BUY", "SELL"}


class TransactionIn(BaseModel):
    """A trade as the page submits it, in the currency it was dealt in.

    Amounts are Decimal all the way in: a fund quantity can carry twelve
    significant figures, and a float round-trip loses the last of them.
    """

    type: EnterableType
    account_id: int = 1
    instrument: str | None = Field(default=None, description="Import key, ISIN or symbol")
    trade_date: date
    settlement_date: date | None = None
    quantity: Decimal | None = Field(default=None, gt=0)
    price_per_unit_local: Decimal | None = Field(default=None, ge=0)
    gross_amount_local: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fees_local: Decimal = Field(default=ZERO, ge=0)
    #: False records "the broker charged something and I do not know what",
    #: which is a different and more honest statement than a fee of zero.
    fees_known: bool = True
    tax_withheld_local: Decimal = Field(default=ZERO, ge=0)
    fx_rate: Decimal | None = Field(default=None, gt=0, description="Quote units per 1 EUR")
    orig_acquire_date: date | None = None
    orig_cost_local: Decimal | None = Field(default=None, ge=0)
    note: str | None = None

    #: How much of this purchase was funded by money arriving from outside --
    #: a salary transfer, a bonus, a gift. Recorded as a separate DEPOSIT
    #: alongside the trade.
    #:
    #: It is asked rather than deduced. This tool used to infer a contribution
    #: whenever a purchase exceeded the cash it knew about, and on real data
    #: that invented 85% of the savings figure: the money might equally have
    #: come from an account it cannot see. Only the person knows, so only the
    #: person says.
    new_money_local: Decimal = Field(default=ZERO, ge=0)
    new_money_label: str | None = Field(default=None, max_length=40)


class Problem(BaseModel):
    code: str
    message: str
    field: str | None = None
    #: An endpoint that would resolve this, for the page to offer as a button.
    remedy: str | None = None


@dataclass
class Checked:
    """The outcome of validating, before it is turned into a response."""

    errors: list[Problem] = field(default_factory=list)
    warnings: list[Problem] = field(default_factory=list)
    instrument: sqlite3.Row | None = None
    account: sqlite3.Row | None = None
    currency: str = "EUR"
    rate: Decimal | None = None
    rate_source: str | None = None
    rate_asof: date | None = None
    gross_local: Decimal | None = None
    sale: scenario.SaleScenario | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(
        self,
        code: str,
        message: str,
        *,
        field_: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.errors.append(Problem(code=code, message=message, field=field_, remedy=remedy))

    def warn(self, code: str, message: str, *, field_: str | None = None) -> None:
        self.warnings.append(Problem(code=code, message=message, field=field_))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _resolve_instrument(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM instrument WHERE import_key = ? OR isin = ? OR symbol = ?",
            (key, key.upper(), key),
        ).fetchone(),
    )


def check(conn: sqlite3.Connection, body: TransactionIn, *, person_id: int = 1) -> Checked:
    """Everything wrong with this trade, and what it would do if saved.

    Collects rather than raises: a form wants the whole list at once, not the
    first objection followed by another round trip for the second.
    """
    out = Checked()

    account = conn.execute("SELECT * FROM account WHERE id = ?", (body.account_id,)).fetchone()
    if account is None:
        out.error("ACCOUNT_UNKNOWN", f"There is no account {body.account_id}.", field_="account_id")
    else:
        out.account = account

    # -- the instrument ----------------------------------------------------
    if body.type in NEEDS_INSTRUMENT and not body.instrument:
        out.error(
            "INSTRUMENT_REQUIRED",
            f"A {body.type.lower()} has to say which holding it is.",
            field_="instrument",
        )
    elif body.type in FORBIDS_INSTRUMENT and body.instrument:
        out.error(
            "INSTRUMENT_FORBIDDEN",
            f"A {body.type.lower()} moves cash and is not attached to a holding.",
            field_="instrument",
        )
    elif body.instrument:
        found = _resolve_instrument(conn, body.instrument)
        if found is None:
            out.error(
                "INSTRUMENT_UNKNOWN",
                f"Nothing here is called {body.instrument!r}.",
                field_="instrument",
                remedy="POST /api/watchlist",
            )
        else:
            out.instrument = found

    out.currency = body.currency or (
        str(out.instrument["currency"])
        if out.instrument is not None
        else str(account["currency"])
        if account is not None
        else "EUR"
    )

    # A database trigger rejects a price whose currency differs from its
    # instrument's, with a message nobody could act on. Caught here instead.
    if out.instrument is not None and body.currency and body.currency != out.instrument["currency"]:
        out.error(
            "CURRENCY_MISMATCH",
            f"{out.instrument['import_key']} is dealt in {out.instrument['currency']}, "
            f"not {body.currency}.",
            field_="currency",
        )

    # -- shape -------------------------------------------------------------
    if body.type in NEEDS_QUANTITY:
        if body.quantity is None:
            out.error("QUANTITY_REQUIRED", "How many units?", field_="quantity")
        if body.price_per_unit_local is None and body.gross_amount_local is None:
            out.error(
                "PRICE_REQUIRED",
                "Enter either a price per unit or the total amount.",
                field_="price_per_unit_local",
            )

    if body.trade_date > date.today():
        out.error(
            "FUTURE_DATE",
            "This tool records what happened, not what is planned.",
            field_="trade_date",
        )

    if body.type == "TRANSFER_IN" and body.orig_acquire_date is None:
        # Without it FIFO restarts the clock and the cost basis is wrong for
        # every subsequent sale of the holding.
        out.warn(
            "ORIGINAL_BASIS_MISSING",
            "Without the original purchase date and cost, this transfer looks "
            "like a fresh purchase and the gain on any later sale will be wrong.",
            field_="orig_acquire_date",
        )

    # -- the amount --------------------------------------------------------
    gross = body.gross_amount_local
    if gross is None and body.quantity is not None and body.price_per_unit_local is not None:
        gross = quantize_money(body.quantity * body.price_per_unit_local)
    out.gross_local = gross

    # -- the exchange rate -------------------------------------------------
    pinned = ledger.rate_for(conn, out.currency, body.trade_date, supplied=body.fx_rate)
    if pinned is not None:
        out.rate, out.rate_source, out.rate_asof = pinned
    else:
        # Refused, never invented. A fabricated rate produces a euro cost
        # basis that looks exact and is not, and it would be frozen into
        # every gain computed from this lot for as long as it is held.
        out.error(
            "FX_UNAVAILABLE",
            f"No {out.currency} exchange rate is stored for {body.trade_date}, "
            "and one will not be guessed. Update rates, or type the rate "
            "from your contract note.",
            field_="fx_rate",
            remedy="POST /api/updates/run",
        )

    if not body.fees_known and body.type in NEEDS_QUANTITY:
        out.warn(
            "FEES_UNKNOWN",
            "With no dealing fee recorded, every gain computed from this trade "
            "is slightly too high.",
            field_="fees_local",
        )

    # -- duplicates --------------------------------------------------------
    if out.instrument is not None and body.quantity is not None:
        twin = conn.execute(
            "SELECT id, trade_date FROM transaction_ WHERE instrument_id = ? AND type = ? "
            "AND account_id = ? AND trade_date = ? AND abs(CAST(quantity AS REAL) - ?) < 1e-9",
            (
                out.instrument["id"],
                body.type,
                body.account_id,
                body.trade_date.isoformat(),
                float(body.quantity),
            ),
        ).fetchone()
        if twin is not None:
            out.warn(
                "DUPLICATE_SUSPECT",
                f"An identical {body.type.lower()} is already recorded (entry {twin['id']}). "
                "Saving this would double the position.",
            )

    # -- what a sale would actually do -------------------------------------
    if body.type == "SELL" and out.instrument is not None and body.quantity is not None:
        _price_the_sale(conn, body, out, person_id=person_id)

    return out


def _price_the_sale(
    conn: sqlite3.Connection, body: TransactionIn, out: Checked, *, person_id: int
) -> None:
    """Run the real FIFO and tax chain over the proposed sale."""
    price = body.price_per_unit_local
    if price is None and body.gross_amount_local is not None and body.quantity:
        price = body.gross_amount_local / body.quantity

    try:
        out.sale = scenario.sell(
            conn,
            instrument=str(out.instrument["import_key"]) if out.instrument else "",
            quantity=body.quantity,
            person_id=person_id,
            as_of=body.trade_date,
            tax_year=body.trade_date.year,
            price_local=price,
            fees_local=body.fees_local,
        )
    except scenario.ScenarioError as exc:
        message = str(exc)
        code = "OVERSELL" if "only" in message and "held" in message else "SALE_IMPOSSIBLE"
        out.error(code, message, field_="quantity")
    except tax.TaxDataMissing as exc:
        # The sale itself is fine; only its tax consequence is unknowable.
        # Refusing the whole trade over a rate the government has not
        # published yet would be the wrong trade-off.
        out.warn("TAX_DATA_MISSING", f"{exc} The trade can still be recorded.")

    # FIFO reads `lot.qty_remaining` as it stands now. For a sale dated before
    # trades already recorded, the real answer is only known after the rebuild.
    if out.instrument is not None:
        later = conn.execute(
            "SELECT count(*) AS n FROM transaction_ WHERE instrument_id = ? AND trade_date > ?",
            (out.instrument["id"], body.trade_date.isoformat()),
        ).fetchone()["n"]
        if later:
            out.warn(
                "BACKDATED_FIFO",
                f"{later} later entries already exist for this holding. The lots "
                "shown are today's; the figures after saving are the authoritative ones.",
            )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def sale_json(result: scenario.SaleScenario) -> dict[str, Any]:
    """The sale preview, flattened for the page.

    `GET /api/scenario/sell` returns the same figures in an older, nested
    shape that predates this module and has tests written against it. Rather
    than reshape a published contract, the page asks *this* endpoint for both
    the trade form and the "what would selling cost" panel on a holding, so
    there is one shape in the front end and one serialiser behind it.
    """
    return {
        "instrument": result.instrument,
        "as_of": result.as_of.isoformat(),
        "quantity": float(result.quantity),
        "quantity_held": float(result.quantity_held),
        "price_local": float(result.price_local),
        "price_asof": result.price_asof.isoformat(),
        "price_stale_days": result.price_stale_days,
        "currency": result.currency,
        "fx_rate": float(result.fx_rate),
        "proceeds_eur": float(result.proceeds_eur),
        "fees_eur": float(result.fees_eur),
        "cost_basis_eur": float(result.cost_basis_eur),
        "gross_gain_eur": float(result.gross_gain_eur),
        "vap_offset_eur": float(result.vap_offset_eur),
        "teilfreistellung_eur": float(result.teilfreistellung_eur),
        "taxable_gain_eur": float(result.taxable_gain_eur),
        "loss_pot": result.pot,
        "in_tax_scope": result.in_tax_scope,
        "allowance_remaining_before_eur": float(result.allowance_remaining_before_eur),
        "allowance_remaining_after_eur": float(result.allowance_remaining_after_eur),
        "tax_this_sale_adds_eur": float(result.marginal_tax_eur),
        "net_proceeds_eur": float(result.net_proceeds_eur),
        "consumes": [
            {
                "lot_id": c.lot_id,
                "opened": c.open_date.isoformat(),
                "quantity": float(c.quantity),
                "cost_basis_eur": float(c.cost_basis_eur),
                "proceeds_eur": float(c.proceeds_eur),
                "gross_gain_eur": float(c.gross_gain_eur),
                "vap_offset_eur": float(c.vap_offset_eur),
                "holding_days": c.holding_days,
            }
            for c in result.consumed
        ],
        "caveats": result.caveats,
    }


def _normalised(conn: sqlite3.Connection, body: TransactionIn, checked: Checked) -> dict[str, Any]:
    """The row exactly as it would be written, both currency legs."""
    gross_local = checked.gross_local
    leg = ledger.freeze(
        conn,
        currency=checked.currency,
        when=body.trade_date,
        gross_local=gross_local,
        fees_local=body.fees_local,
        supplied_rate=body.fx_rate,
    )
    rate = leg.rate
    gross_eur = float(leg.gross_eur) if leg.gross_eur is not None else None
    fees_eur = float(leg.fees_eur) if leg.fees_eur is not None else None

    return {
        "type": body.type,
        "account_id": body.account_id,
        "instrument": (
            str(checked.instrument["import_key"]) if checked.instrument is not None else None
        ),
        "trade_date": body.trade_date.isoformat(),
        "settlement_date": body.settlement_date.isoformat() if body.settlement_date else None,
        # Strings, not floats: the edit form posts these back verbatim, and a
        # twelve-figure fund quantity does not survive a float round trip.
        "quantity": plain(body.quantity) if body.quantity is not None else None,
        "price_per_unit_local": (
            plain(body.price_per_unit_local) if body.price_per_unit_local is not None else None
        ),
        "currency": checked.currency,
        "gross_amount_local": plain(gross_local) if gross_local is not None else None,
        "gross_amount_eur": gross_eur,
        "fees_local": plain(body.fees_local),
        "fees_eur": fees_eur,
        "fees_known": body.fees_known,
        "fx_rate": plain(rate) if rate is not None else None,
        "fx_rate_source": leg.source,
        "fx_rate_asof": leg.as_of.isoformat() if leg.as_of else None,
        "flow_class": FLOW_CLASS[body.type],
        "source": "MANUAL",
        "confidence": "ACTUAL",
    }


def _effect(conn: sqlite3.Connection, body: TransactionIn, checked: Checked) -> dict[str, Any]:
    """The position before and after, so the change is visible without arithmetic."""
    if checked.instrument is None or body.quantity is None:
        return {}
    held = conn.execute(
        "SELECT COALESCE(SUM(CAST(qty_remaining AS REAL)), 0) AS q FROM lot "
        "WHERE instrument_id = ? AND account_id = ?",
        (checked.instrument["id"], body.account_id),
    ).fetchone()["q"]
    delta = float(body.quantity) * (-1 if body.type in ("SELL", "TRANSFER_OUT") else 1)
    return {
        "position": {
            "instrument": str(checked.instrument["import_key"]),
            "quantity_before": held,
            "quantity_after": held + delta,
        }
    }


def _response(conn: sqlite3.Connection, body: TransactionIn, checked: Checked) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": checked.ok,
        "errors": [e.model_dump() for e in checked.errors],
        "warnings": [w.model_dump() for w in checked.warnings],
        "normalised": _normalised(conn, body, checked),
        "effect": _effect(conn, body, checked),
    }
    if checked.sale is not None:
        out["sale"] = sale_json(checked.sale)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/transactions/validate")
def validate(
    body: TransactionIn,
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """What this trade would do, without doing it.

    Always 200, even for a trade that cannot be saved: the verdict is in
    ``ok``, and putting it in the status code would make a form that validates
    as you type impossible to write.

    Takes the read-only connection, which is not decoration -- it is what makes
    "this writes nothing" true by construction rather than by review.
    """
    return _response(conn, body, check(conn, body, person_id=person_id))


def _record_new_money(
    conn: sqlite3.Connection, body: TransactionIn, checked: Checked
) -> int | None:
    """Book the part of a trade that was funded from outside.

    Dated the same day as the trade, which is a stated convention rather than
    an observation: the money may have arrived earlier and waited. The trade
    date is the one thing that is certain here.

    The conversion itself is `core.ledger`'s -- this module hands over a
    validated amount and a rate already pinned to a date, and converts nothing
    (ADR 0004, enforced by tests/test_fx.py).
    """
    if body.new_money_local <= ZERO:
        return None
    return ledger.record_new_money(
        conn,
        account_id=body.account_id,
        on=body.trade_date,
        currency=checked.currency,
        amount_local=body.new_money_local,
        rate=checked.rate if checked.rate is not None else Decimal(1),
        label=body.new_money_label,
    )


def _insert(conn: sqlite3.Connection, body: TransactionIn, checked: Checked) -> int:
    """Hand the validated trade to `core.ledger`, which owns the write.

    The euro leg is frozen there rather than here: converting an amount is only
    legitimate where a rate is pinned to a stated date, and that rule is
    enforced by module, not by intention. See ADR 0004 and core/ledger.py.
    """
    leg = ledger.freeze(
        conn,
        currency=checked.currency,
        when=body.trade_date,
        gross_local=checked.gross_local,
        fees_local=body.fees_local,
        supplied_rate=body.fx_rate,
    )
    entry = ledger.Entry(
        account_id=body.account_id,
        type=body.type,
        trade_date=body.trade_date,
        currency=checked.currency,
        instrument_id=(int(checked.instrument["id"]) if checked.instrument is not None else None),
        settlement_date=body.settlement_date,
        quantity=body.quantity,
        price_per_unit_local=body.price_per_unit_local,
        gross_amount_local=checked.gross_local,
        fees_local=body.fees_local,
        tax_withheld_local=body.tax_withheld_local,
        fees_known=body.fees_known,
        orig_acquire_date=body.orig_acquire_date,
        orig_cost_local=body.orig_cost_local,
        note=body.note,
    )
    return ledger.insert(conn, entry, leg)


#: All three belong to the ledger, not to the HTTP layer. Aliased rather than
#: wrapped so there is one implementation and one place to change it.
_log = ledger.log
_txn_json = ledger.row_json
_delete_rows = ledger.delete


def _guard_editable(conn: sqlite3.Connection, txn_id: int) -> sqlite3.Row:
    """Refuse to touch anything that is not a hand-entered row."""
    row = conn.execute("SELECT * FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"There is no entry {txn_id}.")
    if row["source"] != "MANUAL":
        raise HTTPException(
            409,
            f"Entry {txn_id} came from {row['source'].lower()}, not from this page. "
            "Editing it would be undone by the next import. Correct the source "
            "file instead, or delete this and enter it by hand.",
        )
    linked = conn.execute(
        "SELECT id FROM pending_entry WHERE resulting_txn_id = ? OR source_txn_id = ?",
        (txn_id, txn_id),
    ).fetchone()
    if linked is not None:
        raise HTTPException(
            409,
            f"Entry {txn_id} is tied to proposal {linked['id']} in your inbox. "
            "Resolve that first, so the two cannot disagree.",
        )
    return cast("sqlite3.Row", row)


def _rebuild_after(conn: sqlite3.Connection) -> dict[str, Any]:
    """Recompute the derived layer inside the caller's transaction.

    Synchronous on purpose. Returning ``rebuild_required: true`` and a command
    to run is exactly the pattern that drove the owner of this tool back to a
    terminal; a saved trade that does not show up in the totals is not a saved
    trade as far as anyone using it is concerned.
    """
    result = rebuild.run(conn, end=rebuild.default_end(conn), strict=False, commit=False)
    return {
        "lots_created": result.lots_created,
        "disposals_created": result.disposals_created,
        "position_days": result.position_days,
        "oversold": result.oversold,
        "breaks": result.breaks,
        "ok": result.ok,
    }


@router.post("/api/transactions", status_code=201)
def create(
    body: TransactionIn,
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Record a trade, then recompute everything that depends on it."""
    checked = check(conn, body, person_id=person_id)
    if not checked.ok:
        # Same body shape as validate, so the form renders the failure with
        # the code it already has rather than a second error path.
        raise HTTPException(422, detail=_response(conn, body, checked))

    with transaction(conn):
        txn_id = _insert(conn, body, checked)
        if body.type in ("BUY", "TRANSFER_IN") and checked.instrument is not None:
            conn.execute(
                "UPDATE instrument SET status = 'OWNED' WHERE id = ? AND status <> 'OWNED'",
                (checked.instrument["id"],),
            )
        _log(conn, "CREATE", txn_id, _normalised(conn, body, checked))
        _record_new_money(conn, body, checked)
        rebuilt = _rebuild_after(conn)

    row = conn.execute("SELECT * FROM transaction_ WHERE id = ?", (txn_id,)).fetchone()
    return {
        "id": txn_id,
        "transaction": _txn_json(row),
        "rebuild": rebuilt,
        "warnings": [w.model_dump() for w in checked.warnings],
    }


@router.patch("/api/transactions/{txn_id}")
def update(
    txn_id: int,
    body: TransactionIn,
    conn: sqlite3.Connection = Depends(get_writable_db),
    person_id: int = Query(1, alias="person"),
) -> dict[str, Any]:
    """Correct a hand-entered trade. Imported rows are refused, with a reason."""
    previous = _txn_json(_guard_editable(conn, txn_id))

    checked = check(conn, body, person_id=person_id)
    # The row being replaced is itself a duplicate of the row replacing it.
    checked.warnings = [w for w in checked.warnings if w.code != "DUPLICATE_SUSPECT"]
    if not checked.ok:
        raise HTTPException(422, detail=_response(conn, body, checked))

    with transaction(conn):
        # Delete and re-insert rather than UPDATE: a changed quantity or date
        # invalidates the lots derived from it, and those are rebuilt from
        # scratch anyway. One code path for writing a row, not two.
        _delete_rows(conn, txn_id)
        new_id = _insert(conn, body, checked)
        _log(conn, "UPDATE", new_id, _normalised(conn, body, checked), previous)
        rebuilt = _rebuild_after(conn)

    row = conn.execute("SELECT * FROM transaction_ WHERE id = ?", (new_id,)).fetchone()
    return {
        "id": new_id,
        "replaced": txn_id,
        "transaction": _txn_json(row),
        "rebuild": rebuilt,
        "warnings": [w.model_dump() for w in checked.warnings],
    }


@router.delete("/api/transactions/{txn_id}")
def remove(
    txn_id: int,
    conn: sqlite3.Connection = Depends(get_writable_db),
) -> dict[str, Any]:
    """Delete a hand-entered trade, returning it so the page can offer an undo."""
    previous = _txn_json(_guard_editable(conn, txn_id))

    with transaction(conn):
        _delete_rows(conn, txn_id)
        _log(conn, "DELETE", txn_id, previous, previous)
        rebuilt = _rebuild_after(conn)

    return {
        "deleted": txn_id,
        "was": previous,
        "rebuild": rebuilt,
        "note": "Recorded in the entry log. Re-enter the same values to undo this.",
    }


@router.get("/api/transactions")
def listing(
    conn: sqlite3.Connection = Depends(get_db),
    person_id: int = Query(1, alias="person"),
    instrument: str | None = None,
    type_: str | None = Query(None, alias="type"),
    since: date | None = Query(None, alias="from"),
    until: date | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """The ledger, newest first."""
    where = ["a.person_id = ?"]
    args: list[Any] = [person_id]
    if instrument:
        where.append("(i.import_key = ? OR i.isin = ?)")
        args += [instrument, instrument]
    if type_:
        placeholders = ",".join("?" for _ in type_.split(","))
        where.append(f"t.type IN ({placeholders})")
        args += type_.split(",")
    if since:
        where.append("t.trade_date >= ?")
        args.append(since.isoformat())
    if until:
        where.append("t.trade_date <= ?")
        args.append(until.isoformat())

    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT count(*) AS n FROM transaction_ t JOIN account a ON a.id = t.account_id "
        f"LEFT JOIN instrument i ON i.id = t.instrument_id WHERE {clause}",
        args,
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT t.*, i.import_key, i.name AS instrument_name, i.is_cash_proxy,
               a.name AS account_name
        FROM transaction_ t
        JOIN account a ON a.id = t.account_id
        LEFT JOIN instrument i ON i.id = t.instrument_id
        WHERE {clause}
        ORDER BY t.trade_date DESC, t.id DESC LIMIT ? OFFSET ?
        """,
        [*args, limit, offset],
    ).fetchall()

    # What the page can filter by, drawn from the ledger rather than from the
    # instrument table: something never traded has nothing to filter.
    options = conn.execute(
        """
        SELECT DISTINCT i.import_key, i.name, i.is_cash_proxy, a.name AS account_name
        FROM transaction_ t
        JOIN account a ON a.id = t.account_id
        JOIN instrument i ON i.id = t.instrument_id
        WHERE a.person_id = ?
        ORDER BY i.import_key
        """,
        (person_id,),
    ).fetchall()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {
            "instruments": [
                {
                    "instrument": o["import_key"],
                    "label": (
                        o["account_name"] if o["is_cash_proxy"] else (o["name"] or o["import_key"])
                    ),
                    "is_cash": bool(o["is_cash_proxy"]),
                }
                for o in options
            ],
            "types": [
                r["type"]
                for r in conn.execute(
                    "SELECT DISTINCT t.type FROM transaction_ t "
                    "JOIN account a ON a.id = t.account_id "
                    "WHERE a.person_id = ? ORDER BY t.type",
                    (person_id,),
                )
            ],
        },
        "transactions": [
            {
                "id": r["id"],
                "type": r["type"],
                "trade_date": r["trade_date"],
                "instrument": r["import_key"],
                "instrument_name": r["instrument_name"],
                "account": r["account_name"],
                # A cash proxy is a bookkeeping device, not something anybody
                # bought: `CASH_TRY_3` exists so a deposit account can be
                # valued day by day. Showing that ticker where a fund name
                # belongs makes a deposit look like a mystery holding, so the
                # account it belongs to is shown instead.
                "shown_as": (
                    r["account_name"] if r["is_cash_proxy"] else (r["instrument_name"] or "")
                ),
                "is_cash": bool(r["is_cash_proxy"]),
                "quantity": plain(r["quantity"]) if r["quantity"] is not None else None,
                "price_per_unit_local": (
                    plain(r["price_per_unit_local"])
                    if r["price_per_unit_local"] is not None
                    else None
                ),
                "currency": r["currency"],
                "gross_amount_eur": (
                    float(r["gross_amount_eur"]) if r["gross_amount_eur"] is not None else None
                ),
                "fees_local": plain(r["fees_local"]) if r["fees_local"] is not None else None,
                "fees_known": bool(r["fees_known"]),
                "flow_class": r["flow_class"],
                "source": r["source"],
                "confidence": r["confidence"],
                "note": r["note"],
                # What the page needs to decide whether to offer an edit button.
                "editable": r["source"] == "MANUAL",
            }
            for r in rows
        ],
    }
