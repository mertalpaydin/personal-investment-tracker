# ADR 0006: Portfolio return attribution, and why the residual is gone

**Status:** accepted
**Date:** 2026-08-15
**Amends:** [0004](0004-daily-fx-and-return-decomposition.md)

## Context

The v1 handover listed this as v2 item 3: *"Cariño linking, to remove `residual`
from the FX decomposition. Purely additive; the residual is already stored, so
the test is that it goes to zero."*

That premise turned out to be false, and it is worth recording why rather than
quietly building something else.

ADR 0004 closes with: *"The API returns `linking_method: "chained_with_residual"`
and the residual as a first-class field."* It does not. Nothing in the codebase
has ever emitted that field. `portfolio_daily` has carried
`ret_local_contrib`, `ret_fx_contrib` and `ret_residual` since migration 0001,
and on a fully built database all three are NULL on all 528 rows. No test
referenced them. `git log -S` finds no writer that was later removed.

So the residual was not non-zero. It did not exist, because the decomposition it
belongs to was never built. ADR 0004 recorded an intention as though it were an
outcome — which is the failure mode a decision record is most prone to, and the
reason this one states what was measured.

What v1 actually shipped is the **position-level** split:
`unrealized_eur = unrealized_local_eur + unrealized_fx_eur`, exact, checked by
`doctor`, and summed across positions for `/api/portfolio`. That is a snapshot
of unrealized gain against cost. It is not a return, it says nothing about a
period, and it cannot answer *"of the 56% you made, how much was the lira?"*

## Decision

**Build the portfolio-level attribution, and link it with Cariño so no residual
arises in the first place.**

### Four terms, and the fourth is the honest one

`local` (asset prices) · `currency` (rates) · `income` (distributions and
credited interest) · `unexplained` (everything else).

`unexplained` is trading: fills away from the closing price, fees, quantities
that changed within a day. It is computed as the remainder and **named rather
than distributed**. Folding it into `local` would report execution as stock
selection, and the totals would still reconcile perfectly.

Income earns a term because without one every ex-date lands in `local` as a
price fall with its offsetting credit nowhere — precisely the failure the
distribution work fixed at the ledger level, reintroduced one layer up.

### The same convention as ADR 0004, deliberately

Per position, per day, on the quantity held at the start of the day:

```text
local = Q · (P_t − P_p) / X_t          the price move, converted at today's rate
fx    = Q · P_p · (1/X_t − 1/X_p)      the opening capital, re-translated
────────────────────────────────────
sum   = Q·P_t/X_t − Q·P_p/X_p          exactly the change in euro value
```

This mirrors the level-view ordering: the local gain expressed in today's euros,
the currency effect applied to the original capital. Picking the other ordering
would be equally defensible in isolation and would make the daily terms disagree
with the level terms about the same holding — two correct answers to one
question, which is worse than one.

It also means **there is no cross term**. The price-move-times-rate-move
interaction sits inside the currency leg by construction. A third "interaction"
bucket is a different convention, not a more precise one.

### Cariño linking

Daily contributions add; returns multiply. Chaining them naively understates
this portfolio's return by **11.1 percentage points** — 44.85% against an actual
55.97%, from arithmetic alone, on daily figures that are each individually
correct. Nothing about the wrong answer looks wrong.

Cariño smoothing scales each day by `k_t / K`, where `k_t = ln(1+R_t)/R_t` and
`K = ln(1+R)/R`. Then:

```text
Σ_t R_t · k_t/K  =  Σ_t ln(1+R_t) / K  =  ln(Π(1+R_t)) / K  =  R
```

so any decomposition summing to `R_t` each day sums to `R` over the period,
exactly. Measured residual on the example corpus: **5.9 × 10⁻²⁷**.

The residual is still reported, now as a number that should be zero. A reader
cannot otherwise distinguish a smoothed decomposition from an unsmoothed one,
and `doctor` checks it rather than trusting it.

### Two gaps, not one

Keeping these separate is the substance of this decision.

| | What it is | Fixed by |
|---|---|---|
| **Linking gap** | Correct daily parts that do not sum to the period return | Cariño, exactly |
| **Attribution gap** | Daily parts that do not explain the daily return | Nothing — it is real return, and it is named |

Collapsing them would let the tool report `residual: 0` while folding
unexplained trading into a factor. That is a more convincing lie than reporting
neither.

## Consequences

**Good.** The project's headline claim now exists at portfolio level: on the
example corpus, +81.8% from asset prices and −25.7% from the lira, netting to
+56.0%. Per position that was always visible; across the portfolio it was not
computable.

**Good.** Two independent code paths now produce the period return — the unit
price from valuation, and the compounded daily returns from attribution. They
must agree, `doctor` checks that they do, and neither consults the other.

**Bad.** `build_portfolio_daily` now reads `position_daily` and `cash_daily` a
second time. On 3,385 position-days that is unmeasurable; on ten years of a
larger portfolio it would want an index.

**A limitation, accepted.** A day on which an instrument opens or closes a
position contributes nothing to `local` or `currency` — the whole move lands in
`unexplained`. Attributing it would require an intraday price the system does
not have, and assuming the closing price would report a purchase as
performance.

**A second limitation, accepted.** `unexplained` is a remainder, so it absorbs
any future modelling error silently. Its size is the diagnostic:
`core/attribution.py` raises a caveat above 5% of the total return, and the
example-corpus test pins it below 1%.

## Alternatives rejected

**Report the naive chained sum.** It is wrong by eleven points here and the
error grows with the period. Rejected outright.

**Allocate the linking gap pro rata across the factors.** Cheap, and it makes
the parts sum without any claim that the allocation is right. Cariño is exact
and costs one logarithm per day.

**Three terms with an explicit interaction bucket.** A defensible convention,
and it would contradict the level-view split that already ships. Consistency
with ADR 0004 is worth more than the extra row.

**Leave it, and keep exposing a residual.** What ADR 0004 said it would do, and
what it turned out never to have done. Now that the decomposition exists, an
exact linking method is available for the cost of implementing it.
