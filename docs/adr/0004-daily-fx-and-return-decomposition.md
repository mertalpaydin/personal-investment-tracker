# ADR 0004: Daily FX revaluation, and reporting currency effect separately

**Status:** accepted
**Date:** 2026-08-15

## Context

Fourteen of 36 open lots are denominated in Turkish lira. The inherited brief
assumed euro throughout, with FX applied only at transaction time.

Examining the source export produced the finding that motivates this project.
Solving for the implied EUR/TRY rate on every lira row — `quantity × (current −
open) ÷ reported profit` — returns the same value on every lot:

| lot opened | implied EUR/TRY |
|---|---|
| 2024-07-22 | 55.24 |
| 2024-10-03 | 55.32 |
| 2025-08-31 | 55.25 |
| 2026-03-05 | 55.24 |
| 2026-06-04 | 55.23 |

These lots span two years in which the lira roughly halved. A constant implied
rate is only possible if the profit column computes `(current_price −
open_price) × quantity ÷ today's spot`. It contains **no currency effect at all**.

The practical size of the error, now measured against real ECB rates rather
than estimated. Across the whole lira book of 14 lots:

| | |
|---|---|
| Cost basis at acquisition-date rates | €17,196.58 |
| Market value at today's rate | €19,908.76 |
| **True gain** | **€2,712.18** |
| What the export reports | €5,485.52 |
| **Overstatement** | **€2,773.34 — more than double** |

The largest single lot reports **+€2,107.93** against a true **+€1,087.80**.
And one lot inverts entirely: a position the export shows as **+€36.36** has in
fact **lost €75.57**. A tracker that reports a loss-making position as
profitable is not imprecise, it is giving the wrong answer to the only question
being asked of it.

This also disposes of the brief's Phase 1 acceptance criterion — "matches the
broker's screen to the cent" — for the lira book. Matching that number to the
cent would mean faithfully reproducing a bug.

## Decision

**Revalue every non-euro position daily.** Transaction-date-only conversion is
rejected.

**Three currency rules, enforced structurally rather than by convention:**

1. **One direction only.** `fx_rate.base` is always `'EUR'` and `rate` is always
   quote units per 1 EUR, enforced by a `CHECK` constraint. Conversion is
   *always* division. There is no multiply path, so the single largest bug class
   in multi-currency systems — an inverted rate — cannot be written. The ECB
   publishes in this direction natively; the inverted `TRYEUR=X` quote is not
   used.
2. **Native is authoritative; euro is a stored derivation** carrying the exact
   rate, its as-of date and its source. Prices are never stored in euro, enforced
   by a trigger asserting `price.currency == instrument.currency`.
3. **Conversion is allowed only where a rate is pinned to a date**, and in
   exactly two situations:

   - **Freezing**, at the moment a transaction is recorded — the importer, the
     lot builder (the immutable German *Anschaffungskosten*), and confirming an
     inferred cash flow.
   - **Daily revaluation**, in the position and cash valuation loops.

   Everywhere else — `returns.py`, `tax.py`, the API — contains no FX
   arithmetic at all. That is checkable by grep and is enforced by a test.

   *Amended 2026-08-15:* originally written as "exactly three places", naming
   files. Adding the cash-flow engine showed that a file list is the wrong
   invariant — it has to be re-litigated every time a module is added, which
   invites loosening the test instead of thinking. The rule is about *when* a
   rate is chosen, not which file chooses it.

**Decompose return into local and currency components.** The identity is exact:

```
(1 + r_local) × (1 + r_fx) = 1 + r_eur
```

Logarithmic components are stored, because they are exactly additive across both
legs *and* across time. For display, the local return is reported as
`exp(L_local) − 1` and the currency line as `r_eur − local`, labelled **"currency
effect (incl. cross term)"** — so the two displayed figures sum exactly to the
headline and the label is honest about where the cross term went.

The cross term is large here and must never be split silently. Worked against
the largest lira lot, using its real acquisition rate of 37.7045 and today's
55.3879:

```text
r_loc = 0.627 / 0.318      − 1 = +97.2%    local price return
r_fx  = 37.7045 / 55.3879  − 1 = −31.9%    currency return
r_eur = 1.972 × 0.681      − 1 = +34.3%    what actually happened
cross term = 0.972 × (−0.319)  = −31.0pp   why the two cannot simply be added
```

At position level the same split is available in *level* form with no cross term
at all, via one extra stored column:

```
unrealized_eur       = market_value_eur − cost_basis_eur          (frozen at acquisition FX)
unrealized_local_eur = market_value_eur − cost_basis_eur_at_spot  (= what the export prints)
unrealized_fx_eur    = cost_basis_eur_at_spot − cost_basis_eur
⇒ unrealized_eur = unrealized_local_eur + unrealized_fx_eur       (identity, always)
```

`unrealized_local_eur` is *exactly* the number the source export reports. That is
what lets the API say: "the export shows +€2,108; that is your lira gain
translated at today's rate; your actual euro gain is +€1,088; the difference is
lira depreciation on your original capital."

**The valuation grid is TARGET2 business days** — not Xetra, not the Turkish
fund calendar, not their union. A valuation date is only meaningful if an FX rate
exists for it, and the ECB publishes on exactly that calendar, so `fx_stale_days`
is zero on every grid day. That removes the worst failure mode: a stale rate
freezing a lira position's euro value while the currency moves underneath it.

## Consequences

**Good.** The tracker answers a question the source cannot: how much of a gain is
real and how much is currency. For a portfolio 13% exposed to a rapidly
depreciating currency, that is the difference between a flattering number and a
true one.

**Good.** Because cost basis is frozen at acquisition-date FX from the start,
bringing the lira funds into German tax scope later is a configuration change
rather than a rewrite. German law computes the taxable gain on a foreign fund as
proceeds at sale-date FX minus cost at acquisition-date FX, so the currency
effect is already *part of* the taxable base — the column that makes that
possible has to exist before it is needed.

**Accepted asymmetry in the calendars.** On a Turkish public holiday a valuation
row is still produced: the fund price is one day stale but the FX rate is
current. That is correct — the euro value of a Turkish fund genuinely does move
when the lira does. On a TARGET2 holiday no row is produced at all and the fund's
move lands on the next grid day. Turkish religious holidays run three to five
days and can abut a weekend, which is why the five-day staleness limit is counted
in *grid* days rather than calendar days.

**Honest limitation.** "Local return" means quote-currency return, not economic
exposure. Most of the euro-quoted holdings are dollar-exposed, and v1 reports
their currency contribution as zero. Correct mechanically, misleading
economically. The allocation endpoint states `basis: "quote_currency"` rather
than letting the number speak for itself. One holding is explicitly
currency-hedged, so the right answer varies per instrument and only look-through
resolves it.

**Residual is exposed, not absorbed.** Chain-linked daily contributions do not
reconcile exactly to a compounded period return. The API returns
`linking_method: "chained_with_residual"` and the residual as a first-class
field. Cariño linking is out of v1 scope, and quietly allocating the gap away
would be worse than showing it.

> **Correction, 2026-08-15.** The paragraph above describes an intention, not
> what shipped. No portfolio-level decomposition was ever built: the three
> `ret_*` columns on `portfolio_daily` were never written and the API never
> emitted `linking_method`. Only the *position-level* split in this ADR is
> real. [ADR 0006](0006-return-attribution-and-carino-linking.md) builds the
> period decomposition and links it with Cariño, so the residual is zero rather
> than exposed. Left here rather than edited away, because a decision record
> that quietly corrects itself is not a record.

## Alternatives rejected

**Convert at transaction date only** (the inherited assumption). Cheap, and
wrong: it cannot show currency effect over time, which is the entire point.

**Report only the euro number.** Simpler, and hides exactly the thing the owner
needs to see.

**Match the source export's profit figure.** Rejected: that figure is the bug.
The acceptance criterion is now "we reproduce their number *and* can decompose
exactly why ours differs", which is a stronger correctness claim and, unlike
cent-matching on the lira book, actually true.
