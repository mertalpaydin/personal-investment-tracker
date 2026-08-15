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

The practical size of the error: one gold-fund lot reports a gain of **+€2,105**.
Its actual gain in euro is **+€1,089**. The €1,016 difference is lira
depreciation on the original capital, and it is invisible.

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
3. **Conversion happens in exactly three places**: the importer (freezing
   transaction amounts at trade date), the lot builder (freezing cost basis at
   acquisition-date FX — the immutable German *Anschaffungskosten*), and the
   daily valuation loop. `returns.py`, `tax.py` and the API contain none, which
   is checkable by grep and therefore lintable.

**Decompose return into local and currency components.** The identity is exact:

```
(1 + r_local) × (1 + r_fx) = 1 + r_eur
```

Logarithmic components are stored, because they are exactly additive across both
legs *and* across time. For display, the local return is reported as
`exp(L_local) − 1` and the currency line as `r_eur − local`, labelled **"currency
effect (incl. cross term)"** — so the two displayed figures sum exactly to the
headline and the label is honest about where the cross term went. The cross term
is large here (−31pp on the worked example) and must never be split silently.

At position level the same split is available in *level* form with no cross term
at all, via one extra stored column:

```
unrealized_eur       = market_value_eur − cost_basis_eur          (frozen at acquisition FX)
unrealized_local_eur = market_value_eur − cost_basis_eur_at_spot  (= what the export prints)
unrealized_fx_eur    = cost_basis_eur_at_spot − cost_basis_eur
⇒ unrealized_eur = unrealized_local_eur + unrealized_fx_eur       (identity, always)
```

`unrealized_local_eur` is *exactly* the number the source export reports. That is
what lets the API say: "the export shows +€2,105; that is your lira gain
translated at today's rate; your actual euro gain is +€1,089; the difference is
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

## Alternatives rejected

**Convert at transaction date only** (the inherited assumption). Cheap, and
wrong: it cannot show currency effect over time, which is the entire point.

**Report only the euro number.** Simpler, and hides exactly the thing the owner
needs to see.

**Match the source export's profit figure.** Rejected: that figure is the bug.
The acceptance criterion is now "we reproduce their number *and* can decompose
exactly why ours differs", which is a stronger correctness claim and, unlike
cent-matching on the lira book, actually true.
