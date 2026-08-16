# ADR 0008: Tax scenarios, and the forecast this tool will not make

**Status:** accepted
**Date:** 2026-08-16

## Context

The v1 handover listed "scenario / what-if" last, calling it "the most fun and
the least load-bearing". Both halves are right, and the second half is why this
record exists: *what-if* is unbounded, and the obvious version of it — a slider
for assumed market returns, a Monte Carlo cone, a projected portfolio value in
2035 — would undo four ADRs of refusing to invent data.

The original 45-table design specified exactly that (Monte Carlo projection via
block bootstrap). ADR 0003 cut it. Nothing since has made it a better idea.

But there *is* a question the owner asks before acting, it has an exact answer,
and every piece needed to compute it now exists: FIFO lots with a cost basis
frozen at acquisition-date FX, the German capital-gains engine, the two loss
pots, the Sparerpauschbetrag, and — as of ADR 0007 — the Vorabpauschale relief.

> *"If I sold this today, what would it cost me in tax, and how much allowance
> would be left?"*

## Decision

**Build the tax scenarios. Refuse the market forecast.**

### Today's prices only

Every figure is conditional on one input the owner can check: the last observed
close. Nothing assumes a future return, a volatility, or a holding period.
`core/goal.py` already projects on contributions alone and assumes **no
investment return at all** — this is the same rule, applied to a different
question.

A stale price is reported (`price_stale_days`, and a caveat naming the date)
rather than silently used as though it were today's.

### The marginal figure is the answer

A per-sale tax number in isolation is not useful, because the same gain costs
nothing or 26.375% depending only on what the year's allowance has already
absorbed. So `sell()` computes the year's position **with and without** the
sale and reports the difference.

That difference is deliberately the gap between two independently rounded
yearly bills, not a standalone product. On the test fixture it is €184.62 where
`700 × 26.375%` rounds to €184.63 — and €184.62 is what the owner would
actually be charged. A scenario a cent adrift from the return it predicts is a
scenario that will be distrusted on the one occasion it matters.

### It composes the tax engine; it does not reimplement it

Vorabpauschale relief first (reducing a positive gain toward zero, never
deepening a loss), then Teilfreistellung on what remains — the same order,
through the same functions. A second implementation of a tax rule is how the
two come to disagree, and the disagreement would surface as a scenario that
mispredicts a real disposal.

`test_the_scenario_matches_what_actually_booking_the_sale_produces` books the
sale for real, rebuilds, and asserts the two agree to the cent. That test is
the reason the rest is worth reading.

### Read-only, structurally

Nothing writes. The routes take the read-only connection, and a test asserts
they do — write access is something a route asks for. A scenario that left rows
behind would be indistinguishable from a decision afterwards, and the whole
design rests on the ledger containing only what a person confirmed.

### Harvest lists; it does not recommend

`harvest()` reports the year's remaining allowance and what each holding would
realise if sold whole. It stops there. Which holding to sell depends on what
the owner wants to keep owning, which this module knows nothing about — and a
tool that says "sell ZGLD" is making an investment recommendation it has no
basis for.

A holding that cannot be priced or classified is **skipped**, not shown at
zero: a candidate list with a silent hole in it is worse than a shorter one.

## The Teilfreistellung cross-check

Folded in here because it belongs to the same area and became possible only
now. A fund claiming a 30% Teilfreistellung is asserting that its
*Anlagebedingungen* require a 51% equity minimum. With look-through data (ADR
0005) the tool can now notice a fund claiming the equity exemption while
holding mostly bonds.

It is a **warning, never an error**, and the message says why: the rate follows
the fund's terms, not its holdings, and a compliant equity fund can dip below
51% temporarily. But 30% versus 0% is 30% of the tax on the position, and the
classification is one a person typed in by hand.

This is the only place two v2 features check each other.

## Consequences

**Good.** The question the owner actually asks before acting now has an answer,
and it is the payoff of everything built in v2: FIFO, the tax engine, and the
Vorabpauschale relief all show up in one figure.

**Good.** The allowance has a deadline — unused Sparerpauschbetrag expires on 31
December — and `pt scenario harvest` makes that visible while it can still be
acted on.

**Bad.** `harvest()` prices every holding by calling `sell()` once per
instrument, so it is O(holdings) full scenario evaluations. On ten holdings
that is imperceptible; on a thousand it would want a batch path.

**A limitation, accepted.** Every harvest figure sells the *whole* holding.
Selling part of one realises the oldest lots first and usually carries a
different gain per unit, so the list is an upper bound on what each position
would contribute, not a menu of exact amounts. `pt scenario sell --qty` is the
exact answer and the CLI points at it.

**A limitation, inherited.** Fees are absent from the source data, so every
gain here is biased high and every proceeds figure is optimistic. Stated in the
caveats on every response rather than assumed away.

## Alternatives rejected

**Monte Carlo / assumed-return projection.** The original design specified it.
It produces a number with the shape of a forecast and none of the substance,
and there is no honest way to label that.

**Recommending which holding to sell.** One sort and a threshold away, and it
would be investment advice from a tool that knows only the tax.

**Persisting scenarios so they can be compared later.** Tempting, and it would
make a hypothetical indistinguishable from a decision in the one table where
that distinction is load-bearing.

**Making the Teilfreistellung cross-check an error.** It would be wrong. The
statutory rate follows the fund's terms, and the tool has no access to those.
