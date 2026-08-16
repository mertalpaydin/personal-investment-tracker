# Architecture decision records

Each record states a decision, the evidence behind it, and what it costs. They
are written when a choice is made, not reconstructed afterwards, so several
record findings that contradict the original design brief.

| # | Decision | Why it matters |
|---|---|---|
| [0001](0001-positions-export-not-a-ledger.md) | The source is a positions view, not a transaction ledger | One lot can span both files, or appear twice in one and never in the other. Processing them independently gives undersized FIFO lots and a silently wrong tax engine. |
| [0002](0002-decimal-money-and-lossy-quantities.md) | Decimal money; precision graded, not hidden | Four quantities are abbreviated by the source UI. Two are exactly recoverable from market value — and doing so proves the printed values are wrong. |
| [0003](0003-scope-cut.md) | 45 tables and 11 phases cut to 16 tables | Ship a correct core rather than a broad unfinished one. Two features are pulled *forward* because retrofitting them would mean recomputing all history. |
| [0004](0004-daily-fx-and-return-decomposition.md) | Daily FX revaluation; local and currency return reported separately | The source export's foreign-currency profit column contains no currency effect at all. On one lot that is a €1,016 overstatement. |
| [0005](0005-look-through-exposure.md) | Look-through exposure from published constituent lists, kept out of the return decomposition | "EUR" as a currency weight meant *listing venue*, not risk. Exposure is an estimate; the local/FX return split is an identity. Folding one into the other would make the model's error look like a bug. |
| [0006](0006-return-attribution-and-carino-linking.md) | Portfolio return attributed to prices, currency, income and trading; Cariño-linked | 0004 said it exposed a residual; it had never built the decomposition at all. Chaining daily contributions naively understates this portfolio by 11 points, so the linking is the feature. |
| [0007](0007-vorabpauschale-booking-and-basis-relief.md) | The January charge reaches the ledger, and raises the cost basis so it is not taxed twice | v1 could compute the charge and could apply the relief. The middle was missing, so it reached no tax figure. The relief is the half that is invisible when wrong. |

| [0008](0008-tax-scenarios-without-forecasting.md) | Price a sale before making it; refuse the market forecast | The question worth answering — "what would this cost me in tax?" — has an exact answer. The one the original design specified, a Monte Carlo cone, does not. |

| [0009](0009-goal-projection-without-a-forecast.md) | A goal projection with growth in it, still not a forecast | 0008's refusal was over-generalised. "When do I reach the target?" is inherently about the future, and assuming zero return is not neutral — it is wrong in a known direction. |

## Reference material

`../reference/` holds the original design documents, sanitised and banner-marked.
They are design history rather than a description of the code: the German tax
rules survived verbatim and are implemented as written, while the data-model
assumptions did not survive contact with the real export.
