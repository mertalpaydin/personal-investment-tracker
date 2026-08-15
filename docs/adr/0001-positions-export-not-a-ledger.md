# ADR 0001: The source is a positions view, not a transaction ledger

**Status:** accepted
**Date:** 2026-08-15

## Context

The project's only historical record is an export from a portfolio-tracking site,
in two files: `open.csv` (36 rows) and `closed.csv` (7 rows).

The design brief in `docs/reference/implementation-brief.md` §3 assumed a broker
transaction log and specified a generic column-mapping importer driven by a YAML
config. Examining the actual files shows that assumption is wrong in a way no
column mapping can repair.

Every row in both files says `BUY` — including the closed ones. There are no
`SELL` rows, no fees, no dividends, no cash movements and no transaction
identifiers. Each row is a **lot state as of the export date**, not an event.

Three specific consequences were measured in the real data:

1. **A row in `open.csv` reports the lot's *remaining* quantity**, not the
   quantity originally purchased.
2. **A single lot can appear in both files.** `EGV2 2025-06-13 @ 101.88` shows
   125.726 open and 70.5778 closed. Neither number is the purchase; the purchase
   was 196.3038.
3. **A single lot can appear twice inside `closed.csv` and never in
   `open.csv`.** `IS0E 2025-05-07 @ 20.46` has two disposals, 244.7586 and 244.
   The purchase was 488.7586, and nothing in either file states it.

Case 3 was not anticipated by the brief at all.

## Decision

Treat the pair of files as a **lot-state snapshot** and reconstruct the ledger.
The unit of identity is the lot, keyed by `(symbol, open_date, open_price)`, and
the entire reconstruction is one rule:

```
buy_quantity(key) = open_remaining(key) + Σ closed_quantity(key)
```

That single rule covers all three cases uniformly — the 30-odd ordinary lots, the
both-files split, and the multi-disposal lot with no open row. Each row in
`closed.csv` additionally yields a `SELL` event.

The generic YAML column-mapper is abandoned. A column mapping describes one file;
this problem is a join across two.

## Consequences

**Good.** Lot reconstruction becomes a single testable function with no special
cases. Both real-world oddities fall out of the same `defaultdict` grouping
rather than needing detection logic.

**Bad.** Two genuinely distinct purchases of the same instrument on the same date
at the same price merge into one lot. Under FIFO with identical acquisition date
and identical cost basis this is arithmetically indistinguishable from the true
history, so it is lossless for tax purposes — but it *is* a merge, and the import
report should say so.

**Dangerous, and guarded against.** If the upstream site ever prunes old closed
rows, `Σ closed_quantity` shrinks and the reconstructed purchase would silently
shrink with it, retroactively deleting history. A genuine partial sale moves
quantity from the open side to the closed side, leaving the sum flat, so a
*falling* sum is never legitimate. The importer therefore treats a reconstructed
buy quantity as monotonic: it may grow, never shrink, and a shrink raises
`BUY_QTY_SHRANK` for a human to adjudicate rather than being applied.

**Unavoidable.** Fees, dividends and cash movements are absent from the source
and cannot be recovered by any amount of care. See ADR 0002 for what that costs.

## Notes

The importer's `external_id` scheme follows from this framing. The BUY identifier
must **not** include quantity — an open lot's printed quantity shrinks whenever
it is partially sold, so a quantity-keyed identifier would mint a fresh row on
the next export and duplicate the position. SELL identifiers must include close
date, price and quantity, plus an occurrence index, because one lot legitimately
carries several disposals and two of them can be byte-identical.
