# ADR 0007: Booking the Vorabpauschale, and the relief that stops double taxation

**Status:** accepted
**Date:** 2026-08-16

## Context

v1 could compute the §18 InvStG charge and could apply the relief for it. It
could do nothing in between.

Verified before planning, which is the practice ADR 0006 exists to enforce:

| Link | v1 state |
|---|---|
| `tax.forecast_vorabpauschale` computes the charge | **exists**, tested against hand-computed fixtures |
| Persist it to the `vorabpauschale` table | nothing wrote it |
| Propose it (`pending_entry` kind `VORABPAUSCHALE`) | nothing proposed it |
| Book it (`inbox.BOOKABLE`) | absent — `NotBookable` by design |
| Accumulate it on the lot (`lot.vap_accumulated_eur`) | column existed, **written by nothing** |
| Carry it to the disposal (`lot_disposal.vap_offset_eur`) | column existed, **written by nothing** |
| Apply it as relief in the tax engine | **exists**, tested |

Both ends were real and the middle was missing, so the charge reached no tax
figure at all. The `NotBookable` refusal was correct to ship — reporting success
while recording nothing is the worst available answer — but it had become the
only thing standing between the owner and a number they owe.

## Decision

**Complete the chain, and treat the basis relief as the load-bearing half.**

### A Vorabpauschale is income that moves no money

The booked transaction carries `type = 'VORABPAUSCHALE'` and
`flow_class = 'INCOME'`. Nothing in the cash engine, the unit series, the
attribution or the external-flow reconciliation looks at that type, and that
silence is deliberate and checked: no euro entered or left the portfolio and no
value changed. It is a tax event wearing a ledger row.

Booking it as a `DIVIDEND` instead — the tempting reuse, since both are income —
would credit cash that does not exist and report a return the portfolio did not
earn. That is invariant 2's failure mode with the sign flipped.

### The gross is stored; the tax engine applies Teilfreistellung

Symmetric with a real distribution, so one module knows the exemption rate.
Storing the net would also halve the basis relief, which deducts the gross from
the gain *before* the exemption applies.

### The relief is why this feature exists

A Vorabpauschale is taxed when charged. If it is not then deducted from the gain
when the fund is sold, **the same euros are taxed twice** — and both figures are
individually correct, nothing reconciles them, and the overpayment surfaces
years later in a number nobody can trace. On a €10,000 holding charged €178.50
and later sold at a €3,000 gain, the relief is worth €124.95 of taxable income.
Over a decade of accumulating funds it compounds.

`§ 20 Abs. 1 Nr. 3 Satz 5 EStG` is the relief. The implementation is the two
columns v1 declared and never filled.

### Allocation is chronological, and pro rata is a stated convention

`lots.py` gains a third transaction category beside opening and closing:
`ADJUSTING`, which changes a lot's basis without changing its quantity. It is
processed in the same chronological pass, so **a charge reaches only the lots
open on the day it was raised** — a holding bought in March cannot carry a
January charge, and giving it one would create relief out of nothing.

Within those lots the charge is split **pro rata by quantity still held**. That
is a convention, not an observation, and it is forced: the schema aggregates the
charge per `(account, instrument, tax_year)` while the relief attaches to lots,
which are *derived* and rebuilt from scratch — so a ledger row cannot reference
a lot without pointing a fact at a derivation (invariant 8). The convention
changes *which* future sale collects the relief, never how much exists.

Selling part of a lot takes the same fraction of its accumulated relief. Taking
all of it would give the first partial sale relief for units it did not dispose
of and leave the remainder with none.

### It reaches the tax base, and says so separately

`allowance_state` gains `vorabpauschale_eur` inside `gross_gains_eur`, and both
the CLI and `/api/tax/allowance` break it out. It is the one component on which
tax is due without any money having arrived, and a reader will not expect it.

## Consequences

**Good.** The charge is now computable, reviewable, bookable and relieved, and
the tax figure includes it. `pt vorabpauschale check` reports the relief sitting
on open lots — every euro there is a euro of gain that will not be taxed again.

**Good.** Every kind the `pending_entry` schema allows now has a booking rule.
The `NotBookable` guard stays, and `test_api.py` removes a rule to exercise it
rather than deleting the test along with the gap it described.

**Bad.** `rebuild_lots` now depends on booked Vorabpauschale, so the derived
layer has one more input. It remains a pure function of `transaction_`, which is
what matters, but a rebuild bounded to a past date will legitimately show less
relief than one bounded to today.

**A limitation, accepted.** A charge booked when nothing is held has no basis to
attach to and the relief is lost. It is reported (`RebuildReport.orphan_vap`)
rather than carried forward onto a later purchase, because attaching it to a lot
bought afterwards would invent relief the statute does not give.

**A limitation, accepted.** The pro-rata allocation loses the per-lot month
pro-ration that `forecast_vorabpauschale` applies when computing the charge. The
total is right and the split is defensible; a lot-level charge would need the
ledger to name lots, which the design does not allow.

**Still not automated: nothing schedules this.** `pt vorabpauschale propose` is
run by a person, in January, and confirmed by a person. The Basiszins is
published once a year by the Bundesfinanzministerium and the Teilfreistellung
follows each fund's Anlagebedingungen rather than its holdings — neither is
something the tool can verify, and both are things worth looking at before a
number reaches an *Anlage KAP*.

## Alternatives rejected

**Book it as a DIVIDEND.** Reuses an existing rule, and credits cash that does
not exist.

**Store the taxable figure rather than the gross.** Simpler at the API boundary,
and it would silently halve the relief.

**Reference the lot from the ledger row.** Would preserve the per-lot
pro-ration, and would make a fact depend on a derivation that is deleted and
rebuilt on every `pt rebuild`.

**Auto-confirm the charge.** It is arithmetic on a statutory rate, so the
temptation is real. Rejected: it rests on a rate published annually and a
classification the tool cannot check, and it is the one inference whose output
goes on a tax return.
