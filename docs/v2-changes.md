# What v2 changed

Per-feature notes for the four things v2 built. Read one when you are about to
touch that feature; the reasoning behind each decision is in its ADR, and the
durable rules everything rests on are in [`HANDOVER.md`](HANDOVER.md).

| Feature | ADR | Schema |
|---|---|---|
| [Look-through](#look-through) | [0005](adr/0005-look-through-exposure.md) | migration 0009, two tables |
| [Return attribution](#return-attribution) | [0006](adr/0006-return-attribution-and-carino-linking.md) | migration 0010, one column |
| [Vorabpauschale](#the-vorabpauschale-chain) | [0007](adr/0007-vorabpauschale-booking-and-basis-relief.md) | none — columns existed since 0001 |
| [Tax scenarios](#tax-scenarios) | [0008](adr/0008-tax-scenarios-without-forecasting.md) | none |
| [Goal projection](#goal-projection) | [0009](adr/0009-goal-projection-without-a-forecast.md) | none |

---

## Look-through

Schema 9, two tables, one new CLI group, three API changes. ADR 0005 has the
reasoning; this is what a maintainer needs to know.

### The shape

`fund_holding_set` is one row per (fund, publication date): source, raw weight
sum, the unit the file used, a SHA-256 of the bytes. `fund_holding` is the
lines, weights stored as **fractions of fund NAV**, converted exactly once at
parse time. A composite FK with `ON DELETE CASCADE` means a set and its lines
cannot come apart.

Exposure is computed **on read**. There is no derived table, nothing to rebuild,
and `pt look-through import` says so. Adding a composition changes what the API
reports on the next request.

### Four bases, and why the fourth exists

`look_through` → `declared` → `direct` → `quote_only`. The last one is the v1
answer, kept and named rather than hidden, because a fund reported on its
wrapper's currency and a fund reported on its constituents' currencies are
different claims and must not render alike. Every response carries `coverage`.

`declared` is the old `economic_currency` column, which was dead metadata in v1.
It applies to the currency dimension only — an assertion about currency says
nothing about sector, and inventing one would be indistinguishable from evidence
a week later.

### Traps this added

| Trap | What it does |
|---|---|
| **The weight unit** | `7.24` and `0.0724` are the same weight in two issuers' files. `auto` commits only when the sum is unmistakably near 1 or near 100, and otherwise refuses with both readings printed. This is the `41.5 → 0.00415` trap in a new place. |
| **Normalising at import** | A file summing to 0.62 scaled silently to 1.0 destroys the evidence that it was ever partial. Raw weights and the raw sum are stored; the division happens on read and is reported. |
| **Rows lost after import** | Delete lines from `fund_holding` and the remaining percentages still add to 100% of the *stored divisor*. Nothing downstream notices. `LOOK_THROUGH_ROWS_MISSING` and `LOOK_THROUGH_SUM_MISMATCH` compare the rows against what the file said it contained. |
| **A composition dated after the valuation** | Would back-date today's holdings onto last year's exposure. `composition_date()` is bounded by the valuation date, not by today. |

### Where to extend it

Concentration groups on the identifier when a file supplies one and on the
upper-cased name otherwise — an issuer-mapping table would improve it, and would
be the first place to look if the figures seem low. Nothing yet uses constituent
data for anything but exposure and concentration; overlap-adjusted risk,
issuer-level limits and look-through Teilfreistellung checks are all now
possible and none is built.

---

## Return attribution

Schema 10, one column, one module, two doctor checks. ADR 0006 has the
reasoning and the correction it rests on; this is the operational summary.

### Read the correction first

The v1 handover — this document — said item 3 was "purely additive; the
residual is already stored, so the test is that it goes to zero". It was not
stored. `ret_local_contrib`, `ret_fx_contrib` and `ret_residual` had been
columns since 0001 and were NULL on every row of every built database. ADR 0004
said the API returned `linking_method`; it never did.

The lesson is not about attribution. It is that **an ADR written at decision
time can record an intention as an outcome, and a handover repeating it turns a
plan into a fact.** If §5 or §6 of this document claims something exists, query
for it before you plan around it. It took one `SELECT count(...)` to find this.

### The shape

`core/attribution.py` splits each day's portfolio return four ways and stores
the parts as fractions of that day's opening basis. `build_portfolio_daily`
writes them; nothing else does. The period figures are computed on read.

| Term | What it is |
|---|---|
| `local` | Asset prices, in the currency each is quoted in |
| `currency` | The opening capital, re-translated as rates moved |
| `income` | Distributions and credited interest |
| `unexplained` | The remainder: fills away from the close, fees, quantities that changed within a day |

The convention matches the position-level split in ADR 0004 — local gain at
today's rate, currency effect on the original capital — which is why there is
**no cross term** to distribute.

### The two things that would break quietly

| Trap | What it does |
|---|---|
| **Naive chaining** | Daily contributions add; returns multiply. On this portfolio the gap is 11.1 points — 44.85% against 55.97% — on daily figures that are each correct. Cariño smoothing closes it to 6e-27, and `ATTRIBUTION_LINKING_RESIDUAL` checks that it stayed closed. |
| **A remainder that absorbs a bug** | `unexplained` is computed as whatever is left, so a modelling error lands there silently and every identity still holds. Its *size* is the diagnostic: a caveat fires above 5% of the total, and the example corpus is pinned below 1%. |

`ATTRIBUTION_TOTAL_DISAGREES_WITH_TWR` is the strongest check in the module:
the unit series and the attribution compute the period return from different
data and never consult each other, so agreement is evidence rather than
tautology.

### Per holding

`attribution.by_holding` answers *which holdings* produced the points, since
nobody can sell "currency". On the example corpus ZTFB is the whole story in one
line: +60% from prices, -25% from the lira, netting +35 of the portfolio's 56.

The figures are **percentage points of the portfolio return**, not each
holding's own return — a holding that doubled while being 1% of the portfolio
contributes about one point. Same Cariño factors, so they sum to the
portfolio-level split exactly.

One bug found writing it: the query filtered out the inception day, which
silently dropped the *second* day's contribution too (it is the one paired with
inception). The holdings then summed to 0.7 points more than the portfolio, with
nothing to explain the gap.

### Where to extend it

Attribution is portfolio-wide. Per-instrument and per-currency contributions
are the obvious next cut and need no new storage — the daily terms are already
computed per position and summed before being written, so the change is where
the aggregation happens. Sector attribution needs look-through weights and is
where the two v2 features would finally meet.

---

## The Vorabpauschale chain

No migration. Every column this fills was declared in `0001_core` and written by
nothing: `lot.vap_accumulated_eur`, `lot_disposal.vap_offset_eur`, and the whole
`vorabpauschale` table. ADR 0007 has the reasoning.

### What was actually missing

v1 computed the charge (`tax.forecast_vorabpauschale`) and applied the relief
(`tax.compute_disposal_tax`). Both ends were real and tested. Nothing connected
them, so the charge reached no tax figure at all.

### The half that is invisible when wrong

A Vorabpauschale is taxed when charged. If it is not deducted from the gain when
the fund is sold, **the same euros are taxed twice** — and both figures are
individually correct, so nothing looks wrong until years later.

`lots.py` gained a third transaction category, `ADJUSTING`, which changes a
lot's basis without changing its quantity. Processed in the same chronological
pass, so a January charge reaches only the lots open in January.

| Trap | What it does |
|---|---|
| **Booking it as a DIVIDEND** | Tempting — both are income — and it credits cash that does not exist and reports a return the portfolio never earned. `type = 'VORABPAUSCHALE'` is ignored by the cash engine, the unit series and the attribution, and that silence is tested. |
| **Storing the taxable figure** | The relief deducts the *gross* from the gain before Teilfreistellung, so storing the net silently halves it. The gross is stored and `tax.py` applies the exemption, exactly as for a real distribution. |
| **Relief with nowhere to attach** | A charge booked when nothing is held is lost. Reported as `RebuildReport.orphan_vap` rather than carried onto a later purchase, which would invent relief the statute does not give. |
| **`VAP_RELIEF_LOST`** | The doctor check for the above: every booked euro must sit on an open lot or have been claimed against a disposal. An error, not a warning. |

### The stated convention

The charge is stored per `(account, instrument, tax_year)`; the relief attaches
to *lots*, which are derived. A ledger row cannot name a lot without pointing a
fact at a derivation (invariant 8), so the charge is split **pro rata by
quantity held**. It changes which future sale collects the relief, never how
much exists. Selling part of a lot takes the same fraction of its relief.

### Where to extend it

Nothing schedules `pt vorabpauschale propose`; it is run by a person in January.
A reminder — "the 2025 charge has not been proposed and it is February" — would
be the obvious next step and needs no new data. The per-lot month pro-ration is
lost in the aggregation, which is the one place the arithmetic is coarser than
`forecast_vorabpauschale` can compute.

---

## Tax scenarios

No migration. `core/scenario.py` composes what already exists and writes
nothing. ADR 0008 has the reasoning.

### The scoping was the decision

"What-if" is unbounded. The version the original 45-table design specified --
Monte Carlo projection via block bootstrap -- would have undone four ADRs of
refusing to invent data, and ADR 0003 had already cut it once.

So: **the tax question, which has an exact answer, and not the market question,
which does not.** Every figure is conditional on the last observed close, and a
stale price is named rather than used as though it were today's. There is no
assumed-return parameter anywhere, and adding one would be the single easiest
way to make this tool dishonest.

### Two properties hold it together

| Property | Why it matters |
|---|---|
| **It agrees with the sale it models** | A scenario that mispredicts the disposal it describes is a wrong number with the authority of a computed one. `test_the_scenario_matches_what_actually_booking_the_sale_produces` books the sale for real, rebuilds, and compares to the cent. |
| **The marginal figure is the answer** | The same gain costs nothing or 26.375% depending only on what the year's allowance absorbed. `sell()` computes the year with and without the sale and reports the difference -- deliberately a difference of two rounded yearly bills, not a standalone product, because that is what the owner is actually charged. |

### Watch for

- `harvest()` calls `sell()` once per holding. Fine at ten, wrong shape at a
  thousand.
- Every harvest figure sells the **whole** holding. Partial sales realise the
  oldest lots first and carry a different gain per unit; `pt scenario sell
  --qty` is the exact answer.
- `core/scenario.py` is the **seventh** entry in `test_fx.py`'s conversion
  allowlist, and the test now asserts that count. Before adding an eighth, ask
  why that module knows about currency at all -- `returns.py`, `tax.py` and the
  entire API manage without.

### The one place two v2 features check each other

`doctor.check_teilfreistellung_against_holdings` uses look-through constituents
to notice a fund claiming the 30% equity exemption while holding mostly bonds.
A **warning, never an error**: the rate follows the fund's Anlagebedingungen,
not its holdings, so the tool can prompt but cannot correct.

---

## Goal projection

No migration. `core/projection.py` reads the daily return series that
`portfolio_daily` already stores and writes nothing.

### Why it exists

`core/goal.py` assumes **no investment return at all**. That is falsifiable and
deliberately pessimistic, and for a portfolio that compounds it is wrong in a
known direction: it always names a date too late. The owner asked for the
missing half.

### Corrected after review

The first version bootstrapped the **portfolio's own** return history. That is
contaminated for this purpose: it contains positions since sold, the timing of
past contributions, and weights that describe nothing current — it was
projecting a portfolio that no longer exists. On the example corpus a
fully-closed crypto position that lost 40% was still influencing the forecast.

It now models the **price history of the instruments held today**, at today's
weights, with each instrument's euro return being its local price move combined
with the currency move (ADR 0004's identity applied to a return, not an amount —
which is why this module is not a currency conversion site).

Contributions were also being averaged over all history. They are now measured
over a recent window (6 months by default), **reported as a suggestion with the
window stated, and editable**. A savings rate is a decision, not a constant.

### Two answers, neither a forecast

| | What it is |
|---|---|
| **Sensitivity table** | The reach date at 0/2/4/6/8% a year. Not five forecasts — the statement *"the answer depends on this, and here is how much"*. The tool never picks a row. |
| **Block bootstrap** | Resampling this portfolio's own daily returns in 21-day blocks. A model, labelled as one, with its sample window printed beside every figure it produces. |

The distinction from the Monte Carlo cone ADR 0003 cut: everything here is
either an assumption the owner states outright or an observation this portfolio
made. Nothing is invented.

### Traps found while building it

| Trap | What it does |
|---|---|
| **`r/12`** | Overstates the monthly rate, and the error compounds over a decade of steps. It is `(1+r)^(1/12) - 1`. |
| **Contributions at the start of the month** | Compounds money not yet paid in and flatters every date by a few months. They land at the end and earn nothing that month. |
| **A window where every holding has a price** | Prices are stored from each instrument's first trade, so one recently-bought fund truncates the whole model to its own lifetime. Each day now uses the holdings that existed then, renormalised, and `min_daily_coverage` reports the thinnest day kept. |
| **Overlapping blocks look like more data** | 359 days cut into 252-day blocks yields 108 windows and about 1.4 *independent* observations. `effective_samples` reports the honest number and a `WEAK:` caveat fires below three. |
| **Block length aliasing** | A 21-day block over a 7-day repeating history is exactly three whole cycles, so every block is identical and the bootstrap silently collapses to one path — a sample that looks varied and behaves flat. The test fixture uses a 13-day cycle for this reason, and `test_a_history_with_no_dispersion_produces_none` pins the collapsed case as correct behaviour. |
| **A cone from a flat history** | A bootstrap cannot manufacture uncertainty the sample does not contain, and should not try. |

### Watch for

The example corpus returned **23.7% a year** over 527 days — a window containing
one unusual run. The bootstrap therefore reports dates *earlier* than the 8%
row of the sensitivity table, which is arithmetically correct and exactly why
the sample window is printed next to every decile. If that number ever starts
being treated as a rate to plan on, the feature has failed.
