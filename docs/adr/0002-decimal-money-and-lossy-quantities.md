# ADR 0002: Decimal money, and grading precision instead of hiding it

**Status:** accepted
**Date:** 2026-08-15

## Context

The design brief sets an acceptance criterion in cents: computed portfolio value
must match the statement exactly. Two things in the source data make that harder
than a choice of numeric type.

**Quantities above 100,000 are abbreviated by the source UI.** Four lots print as
`376.9K`, `148.76K`, `102.59K` and `100.45K`. A value shown to one decimal is
known only to ±50 units; to two decimals, ±5. The precision is not recoverable
from the quantity column.

**But it is often recoverable from elsewhere.** `Market Value = quantity ×
Current Price` holds to the cent across all 36 open rows. Where the market value
is *not* itself abbreviated, dividing it by the current price recovers the
quantity exactly — and doing so reveals that the printed quantity is **wrong**:

| printed | recovered | error |
|---|---|---|
| `148.76K` | 148,756 | −4 |
| `100.45K` | 100,445 | −5 |

The other two lots have market values above the same threshold, so they are
abbreviated too and only an interval is available.

**Separately, `Open Price` is rounded to 2dp while `Net P/L` is not.** For euro
rows `cost = Market Value − Net P/L` is exact to the cent and implies a truer
open price than the one displayed; `quantity × open_price` is off by up to €1.57
on a single lot. For lira rows that route is unavailable, because the profit
column is computed at a single spot rate (see ADR 0004).

## Decision

**Money and quantities are `Decimal`, never `float`.** Registered sqlite3
adapters round-trip them as TEXT. A project whose acceptance criterion is stated
in cents cannot represent money in binary floating point.

**Precision is a stored, queryable property rather than a comment.** Two
independent grades, because they fail independently:

- `quantity_confidence` ∈ `OVERRIDE | EXACT | DERIVED | ESTIMATED`
- `basis_quality` ∈ `EXACT | DERIVED | ESTIMATED`

`SELECT * FROM transaction_ WHERE quantity_confidence = 'ESTIMATED'` is then the
honest answer to "how much do you actually own", rather than a caveat buried in a
docstring.

**Resolution order for quantity** is `OVERRIDE > EXACT > DERIVED > ESTIMATED`.
The derivation is computed even when an override exists, and an override falling
outside the derived interval raises `OVERRIDE_CONTRADICTS_DERIVED`. The user may
well be right — but a silent disagreement between a typed number and the
arithmetic is exactly what an import report exists to surface.

## Consequences

**Good.** All four abbreviated lots are open positions, and authoritative
quantities can be supplied for open positions. In practice they all resolve to
`OVERRIDE` and no `ESTIMATED` quantity reaches the database. The two recoverable
lots double as a validation of the derivation itself: if the authoritative
figures match the derived ones, the method is proven for the cases where it is
the only option.

**Structural limits that remain.** The export loses precision above 100,000 units
as a matter of format, so a future *closed* lot in that range would have no
override path — the interval would be the best available answer. And lira cost
basis stays `ESTIMATED` regardless of quantity precision, because the open price
is only ever shown to 3–4 decimals and there is no lossless route to the true
figure.

**The larger, unfixable one.** Fees and dividends are absent from the source
entirely (ADR 0001). Cost basis is therefore understated by every purchase fee
ever paid, which biases every computed taxable gain *high*; and total return is
understated by every distribution received. No numeric type fixes a column that
is not in the file. Dividends must **not** be estimated from a price feed and
booked — inventing a tax event is worse than omitting one. The API carries a
permanent caveat instead, and manual backfill from statements is the remedy.

## Notes

The acceptance criterion in the brief — "matches the statement to the cent" —
is achievable for the euro book and is retained there. It is not achievable for
the lira book, and pursuing it there would mean reproducing a bug. See ADR 0004.
