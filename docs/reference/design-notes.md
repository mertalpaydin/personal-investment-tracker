> **Background and rationale, superseded in places.**
>
> The "why" companion to the implementation brief. Where it conflicts with the
> ADRs in `docs/adr/`, the ADRs win -- they were written after examining the
> real data. Its five design principles, particularly "store facts, derive
> everything else", remain the basis of the current design.

---

# Portfolio Tracker + Peer Advisor — Data Model & Project Plan

> **Status:** background/rationale document. Where this conflicts with `IMPLEMENTATION.md`, that file wins. Resolved since first draft: LLM provider is **Gemini 3.6 Flash (free tier)**, not Claude; ETF minimum fund size **€100m**; stock universe **large-cap index constituents, €5bn market-cap floor**. Authoritative DDL is `schema.sql`.

Target stack: SQLite (single file), Python for ingestion/compute, your UI on top, an LLM behind a provider-agnostic adapter for the narrative layer only.

---

## Part 1 — Design principles

1. **Lot-level from day one.** German tax is FIFO per depot per instrument. If you store aggregate positions you will rewrite everything.
2. **Store facts, derive everything else.** `transaction` and `price` are the only sources of truth. Lots, positions, valuations, returns are all recomputable. This makes the what-if engine nearly free — it's the same pipeline with a modified transaction set.
3. **Two people, two tax universes.** Allowances and loss pots never mix across persons (unless jointly assessed married, which is a flag, not a merge).
4. **Owned and not-owned share one `instrument` table.** The only difference is whether transactions exist. This is what makes peer comparison trivial.
5. **AI reads a JSON view, never the database.** Deterministic SQL produces the comparison; the model writes prose about it.

---

## Part 2 — Schema

### 2.1 Identity & accounts

```sql
CREATE TABLE person (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  church_tax_rate REAL DEFAULT 0.0,      -- 0.08 or 0.09 if applicable
  joint_assessment INTEGER DEFAULT 0     -- 1 = married, jointly assessed
);

CREATE TABLE account (
  id INTEGER PRIMARY KEY,
  person_id INTEGER REFERENCES person(id),
  name TEXT NOT NULL,
  broker TEXT,
  kind TEXT CHECK(kind IN ('DEPOT','CASH','TAGESGELD','FESTGELD')),
  currency TEXT DEFAULT 'EUR',
  freistellungsauftrag_eur REAL DEFAULT 0,   -- amount filed at THIS bank
  opened_date TEXT,
  active INTEGER DEFAULT 1
);
```

Note: the Freistellungsauftrag is filed per bank and the sum across banks per person cannot exceed 1,000 EUR (2,000 joint). The model tracks both the filed split and the statutory cap so you can spot a misallocation — a common and expensive mistake when one bank's FSA is exhausted while another sits unused.

### 2.2 Instrument universe (owned + watched)

```sql
CREATE TABLE instrument (
  id INTEGER PRIMARY KEY,
  isin TEXT UNIQUE,
  wkn TEXT,
  symbol TEXT,                    -- yfinance ticker, e.g. 'EUNL.DE'
  name TEXT NOT NULL,
  asset_class TEXT CHECK(asset_class IN ('ETF','STOCK','CASH','BOND','OTHER')),
  currency TEXT DEFAULT 'EUR',
  exchange TEXT DEFAULT 'XETRA',

  -- fund-only metadata (manual entry from KID/factsheet)
  ter_bps INTEGER,                -- 20 = 0.20% p.a.
  benchmark_index TEXT,
  replication TEXT,               -- physical / sampled / synthetic
  distribution_policy TEXT CHECK(distribution_policy IN ('ACC','DIST')),
  domicile TEXT,                  -- IE, LU, DE ...
  ucits INTEGER DEFAULT 1,

  -- tax
  teilfreistellung_pct REAL DEFAULT 0.0,   -- 0.30 equity fund, 0.15 mixed, 0.0 stock
  is_fund INTEGER DEFAULT 0,               -- subject to Vorabpauschale

  -- classification for peer grouping
  region TEXT, sector TEXT, style TEXT,

  status TEXT CHECK(status IN ('OWNED','WATCH','UNIVERSE','RETIRED')) DEFAULT 'UNIVERSE',
  metadata_verified_at TEXT,
  notes TEXT
);
```

`status` is a convenience label; ownership is always *derived* from open lots. Keep both — the label drives what you fetch daily, the derivation drives what you value.

```sql
-- time-varying attributes, weekly snapshot (screening thresholds live here)
CREATE TABLE instrument_fundamental (
  instrument_id INTEGER REFERENCES instrument(id),
  as_of TEXT,
  fund_size_eur REAL,             -- ETF AUM
  market_cap_eur REAL,            -- stock
  shares_outstanding REAL,
  pe_ratio REAL, dividend_yield REAL,
  source TEXT,
  PRIMARY KEY (instrument_id, as_of)
);
```

### 2.3 Prices & FX

```sql
CREATE TABLE price (
  instrument_id INTEGER REFERENCES instrument(id),
  date TEXT,
  close REAL NOT NULL,            -- raw close; use for tax & NAV
  total_return_close REAL,        -- dividend-adjusted; use for performance
  volume REAL,
  currency TEXT,
  source TEXT,
  PRIMARY KEY (instrument_id, date)
);

CREATE TABLE fx_rate (
  date TEXT, base TEXT, quote TEXT, rate REAL,
  PRIMARY KEY (date, base, quote)
);

CREATE TABLE benchmark_series (
  code TEXT,                      -- 'MSCI_WORLD_PROXY', 'TAGESGELD_3_3', 'MORTGAGE_HURDLE'
  date TEXT, value REAL,
  PRIMARY KEY (code, date)
);
```

Keeping `close` and `total_return_close` separate is not optional. Tax uses raw prices; performance and peer comparison must use total return or distributing ETFs will look permanently worse than accumulating ones.

### 2.4 Transactions — the ledger

```sql
CREATE TABLE transaction_ (
  id INTEGER PRIMARY KEY,
  account_id INTEGER REFERENCES account(id),
  instrument_id INTEGER REFERENCES instrument(id),   -- NULL for cash movements
  type TEXT CHECK(type IN (
    'BUY','SELL','DIVIDEND','INTEREST','FEE','TAX_WITHHELD',
    'DEPOSIT','WITHDRAWAL','VORABPAUSCHALE','SPLIT','TRANSFER_IN','TRANSFER_OUT'
  )),
  trade_date TEXT NOT NULL,
  settlement_date TEXT,
  quantity REAL,
  price_per_unit REAL,
  gross_amount_eur REAL,
  fees_eur REAL DEFAULT 0,
  tax_withheld_eur REAL DEFAULT 0,
  fx_rate REAL DEFAULT 1.0,
  external_id TEXT UNIQUE,        -- broker reference; makes CSV re-import idempotent
  import_batch_id INTEGER,
  note TEXT
);
```

`external_id UNIQUE` is what lets you re-import the same broker CSV without creating duplicates. Worth getting right early.

`TRANSFER_IN` must carry the original acquisition date and cost basis, or FIFO breaks after a depot move.

### 2.5 Tax lots

```sql
CREATE TABLE lot (
  id INTEGER PRIMARY KEY,
  account_id INTEGER, instrument_id INTEGER,
  open_txn_id INTEGER REFERENCES transaction_(id),
  open_date TEXT,
  qty_opened REAL, qty_remaining REAL,
  cost_total_eur REAL,            -- incl. purchase fees
  cost_per_unit_eur REAL,
  vap_accumulated_eur REAL DEFAULT 0   -- Vorabpauschalen already taxed on this lot
);

CREATE TABLE lot_disposal (
  id INTEGER PRIMARY KEY,
  sell_txn_id INTEGER REFERENCES transaction_(id),
  lot_id INTEGER REFERENCES lot(id),
  qty REAL,
  proceeds_eur REAL,              -- net of sale fees
  cost_basis_eur REAL,
  gross_gain_eur REAL,
  vap_offset_eur REAL,            -- accumulated Vorabpauschale, deducted from gain
  teilfreistellung_eur REAL,
  taxable_gain_eur REAL,          -- what actually hits the allowance
  holding_days INTEGER
);
```

`vap_accumulated_eur` is the field most home-built trackers miss. Vorabpauschalen already taxed in prior years are deducted from the gain when you finally sell, otherwise you're double-taxed. Without it your allowance planning drifts every year.

### 2.6 Tax year state

```sql
CREATE TABLE allowance_ledger (
  person_id INTEGER, tax_year INTEGER,
  statutory_cap_eur REAL,         -- 1000, or 2000 if joint_assessment
  PRIMARY KEY (person_id, tax_year)
);

CREATE TABLE allowance_entry (
  id INTEGER PRIMARY KEY,
  person_id INTEGER, tax_year INTEGER,
  source TEXT CHECK(source IN ('DIVIDEND','INTEREST','VORABPAUSCHALE','REALIZED_GAIN')),
  txn_id INTEGER, disposal_id INTEGER,
  amount_eur REAL,                -- post-Teilfreistellung, i.e. allowance actually consumed
  status TEXT CHECK(status IN ('ACTUAL','FORECAST','PLANNED')),
  entry_date TEXT
);

CREATE TABLE loss_pot (
  person_id INTEGER, tax_year INTEGER,
  pot TEXT CHECK(pot IN ('AKTIEN','SONSTIGE')),
  carried_in_eur REAL, additions_eur REAL, offsets_eur REAL, carried_out_eur REAL,
  PRIMARY KEY (person_id, tax_year, pot)
);

CREATE TABLE vorabpauschale (
  id INTEGER PRIMARY KEY,
  account_id INTEGER, instrument_id INTEGER, tax_year INTEGER,
  jan_first_price REAL, year_end_price REAL,
  basiszins REAL,                 -- 0.0320 for 2026
  basisertrag_eur REAL,           -- value × basiszins × 0.7
  distributions_eur REAL,
  months_held INTEGER,            -- 1/12 pro-rating for part-year holdings
  teilfreistellung_eur REAL,
  taxable_eur REAL,
  status TEXT CHECK(status IN ('FORECAST','ACTUAL'))
);
```

The `status` field with `PLANNED` is what makes December harvesting work: you insert hypothetical entries, see remaining headroom, then convert to `ACTUAL` on execution.

Vorabpauschale is capped at the actual price increase over the year — a fund that fell has none. Your forecast should recompute this weekly in Q4.

### 2.7 Peer comparison layer

```sql
CREATE TABLE instrument_performance (
  instrument_id INTEGER, as_of TEXT,
  ret_1m REAL, ret_3m REAL, ret_6m REAL, ret_1y REAL, ret_3y_ann REAL, ret_5y_ann REAL,
  vol_1y REAL, vol_3y REAL,
  max_dd_3y REAL,
  sharpe_3y REAL,
  tracking_diff_1y REAL,          -- ETF only, vs benchmark_index where available
  currency_exposure TEXT,         -- JSON
  PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE risk_bucket (
  instrument_id INTEGER, as_of TEXT,
  bucket TEXT,                    -- e.g. 'EQ_DM_BROAD', 'EQ_EM', 'EQ_SECTOR_TECH', 'BOND_IG_EUR'
  vol_band TEXT,                  -- 'LOW' <10%, 'MID' 10-18%, 'HIGH' >18% (annualised 3y)
  method TEXT,
  PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE screen (
  id INTEGER PRIMARY KEY, name TEXT,
  asset_class TEXT,
  min_fund_size_eur REAL,         -- your ETF threshold
  min_market_cap_eur REAL,        -- your stock threshold
  max_ter_bps INTEGER,
  regions TEXT, sectors TEXT,     -- JSON arrays
  active INTEGER DEFAULT 1
);

CREATE TABLE news_item (
  id INTEGER PRIMARY KEY,
  instrument_id INTEGER, published_at TEXT,
  headline TEXT, source TEXT, url TEXT UNIQUE,
  sentiment REAL,                 -- -1..+1
  sentiment_method TEXT,          -- 'claude' / 'lexicon'
  summary TEXT                    -- your own 1-line summary, not source text
);
```

Store links and your own summaries, not article text — both for copyright and because the DB stays small.

**How the peer scan works, concretely.** A holding is in bucket `EQ_DM_BROAD` / vol band `MID`. SQL finds every non-owned instrument in the same bucket and band passing your active `screen`, ranks by `ret_3y_ann` delta, joins 90-day mean `sentiment`, and emits the top 5 as JSON. Claude receives that JSON and writes: "IWDA sits in the same bucket as your VWCE, returned 1.9%/yr more over 3 years at comparable volatility, TER 0.12% lower, news skew mildly positive." Zero prediction, zero freedom to invent numbers.

### 2.8 Goal, rules, and AI audit

```sql
CREATE TABLE goal (
  id INTEGER PRIMARY KEY,
  name TEXT DEFAULT 'Home purchase',
  target_date TEXT, target_amount_eur REAL,
  mortgage_rate_assumption REAL,  -- 0.037
  buffer_bps INTEGER,             -- 150 = +1.5%
  committed_bucket_target_eur REAL,
  monthly_contribution_eur REAL
);

CREATE TABLE policy_rule (             -- your IPS, machine-checkable
  id INTEGER PRIMARY KEY,
  rule_text TEXT,
  rule_type TEXT,                      -- MAX_POSITION_PCT, MIN_CASH, NO_SINGLE_STOCK_ABOVE, GLIDEPATH
  params TEXT,                         -- JSON
  active INTEGER DEFAULT 1
);

CREATE TABLE ai_run (
  id INTEGER PRIMARY KEY,
  run_type TEXT,                       -- daily_brief / harvest_plan / peer_scan / trade_check
  created_at TEXT, model TEXT,
  input_json TEXT, output_json TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
);
```

`ai_run` pays for itself: you can diff what the model said three months ago against what happened, and you can see your API spend without leaving the app.

### 2.9 Materialised views (rebuilt nightly)

- `position_daily` — account, instrument, date, qty, market value EUR, unrealized gain
- `portfolio_daily` — person/total value, contributions to date, TWR index, XIRR
- `what_if_run` + `what_if_result` — scenario definition (JSON transaction overrides) plus resulting series

---

## Part 3 — Project plan

### Phase 0 — Decisions (before any code)
- Fix your thresholds: ETF minimum fund size, stock minimum market cap, max TER.
- Write the IPS: 5–10 sentences of rules. This is the spine of the whole advisory layer.
- Set target date, target amount, mortgage assumption, committed-bucket size.
- Pick the initial universe: ~30 ETFs, ~50 stocks. Start small; adding is cheap.

### Phase 1 — Ledger and prices
Schema, broker CSV importer (one parser per broker), yfinance daily fetch via GitHub Actions cron after 17:30 CET, FIFO lot builder, `position_daily`.
**Done when:** your computed portfolio value matches your broker's screen to the cent. Do not proceed until it does.

### Phase 2 — Metrics and UI
TWR, XIRR, contribution/growth split, drawdown, progress against the mortgage hurdle, "months of purchase delay" framing.
Backfill history first so the charts have something to show.

### Phase 3 — Tax engine
Allowance ledger, Teilfreistellung, loss pots, Vorabpauschale forecaster (Basiszins 3.20% for 2026, taxable January 2027), harvesting planner with fee break-even.
**Validation:** run last completed year through it and reconcile against your Steuerbescheinigung. Discrepancies here are bugs, not rounding.

### Phase 4 — Universe and peers
Metadata entry, weekly fundamentals snapshot, `instrument_performance` and `risk_bucket` computation, screening UI, news ingestion + sentiment.
**Done when:** the peer scan returns sensible candidates as raw JSON, before any AI touches it.

### Phase 5 — AI layer
Daily brief, weekly peer scan narrative, pre-trade IPS check, December harvest plan. Prompt rule: the model may only reference numbers present in the input JSON, and must decline to comment on future direction.

### Phase 6 — What-if engine
Scenario replay against modified transaction sets, benchmark counterfactuals, per-trade scorecard vs. identical-window benchmark.
Deliberately last — it's the most fun and the least useful, and it depends on everything above being correct.

---

## Part 4 — Known limits

| Want | Reality |
|---|---|
| Auto depot sync | Not available free. CSV import + manual entry. |
| Real-time XETRA | 15-min delay minimum on free sources. EOD is sufficient here. |
| ETF AUM / TER via API | Unreliable. Manual entry, re-verified ~2×/year. |
| Index levels for tracking difference | Licensed. Use peer-relative comparison, or a single free proxy ETF as index stand-in. |
| Exact tax replication | Approximate. Your bank's Steuerbescheinigung is authoritative; this is a planning worksheet. |
| Screening all XETRA ETFs | Not at zero cost. Curated 150–300 instrument universe instead. |
