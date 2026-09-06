/*
 * Instruments you do not own, and how yours compare with them.
 *
 * The comparison is the point. A watchlist on its own is a list of prices; the
 * question worth answering is "is there something with the same kind of risk
 * that has done better, and more cheaply". The server computes that from
 * stored prices and a stated risk category, and this screen shows the working
 * -- the volatility and the worst fall sit beside the return, because a fund
 * that returned more took some risk to do it and hiding that would turn a
 * comparison into a recommendation.
 *
 * Adding is a two-step: check the ticker, then confirm what came back. There
 * is no autocomplete because there is no supported symbol search to build one
 * on, and a search box that breaks silently is worse than a field you type.
 */

import { el, card, row, heading } from "../dom.js";
import { get, post, del, patch, all, isError } from "../api.js";
import * as f from "../fmt.js";
import { timeChart, toTs, palette, indexTo100 } from "../charts.js";
import * as router from "../router.js";
import { explainPanel } from "../narrative.js";
import { sortableTable } from "../tables.js";

/* Plain English for the category codes. The code is what the engine matches
   on; nobody should have to type EQ_GLOBAL_BROAD to be understood. */
const BUCKET_LABEL = {
  EQ_GLOBAL_BROAD: "Shares — whole world",
  EQ_US_LARGE: "Shares — large US companies",
  EQ_EUROPE: "Shares — Europe",
  EQ_EM: "Shares — emerging markets",
  EQ_SECTOR: "Shares — one sector or theme",
  EQ_SINGLE_STOCK: "A single company",
  BOND: "Bonds",
  MONEY_MARKET: "Money market / cash-like",
  COMMODITY: "Commodity, such as gold",
  CRYPTO: "Crypto",
  CASH: "Cash",
  OTHER: "Something else",
};

export const RISK_LEVEL_CONFIG = {
  1: { num: 1, label: "Level 1 · Ultra Low (Cash & Overnight)", bg: "rgba(13, 148, 136, 0.15)", color: "#0f766e" },
  2: { num: 2, label: "Level 2 · Very Low (Short Bonds)", bg: "rgba(22, 163, 74, 0.15)", color: "#15803d" },
  3: { num: 3, label: "Level 3 · Low-Mid (Aggregate Bonds)", bg: "rgba(8, 145, 178, 0.15)", color: "#0e7490" },
  4: { num: 4, label: "Level 4 · Moderate (Core Global Equity)", bg: "rgba(37, 99, 235, 0.15)", color: "#1d4ed8" },
  5: { num: 5, label: "Level 5 · Mid-High (EM & Commodities)", bg: "rgba(217, 119, 6, 0.15)", color: "#b45309" },
  6: { num: 6, label: "Level 6 · High Risk (Sectors & Stocks)", bg: "rgba(234, 88, 12, 0.15)", color: "#c2410c" },
  7: { num: 7, label: "Level 7 · Speculative (Crypto)", bg: "rgba(220, 38, 38, 0.15)", color: "#b91c1c" },
};

export function riskBadge(level) {
  if (!level) return null;
  const cfg = RISK_LEVEL_CONFIG[level] || { label: `Level ${level}`, bg: "rgba(100, 116, 139, 0.15)", color: "#475569" };
  return el("span.tag", {
    text: `Risk ${level}/7`,
    title: cfg.label,
    style: `background: ${cfg.bg}; color: ${cfg.color}; font-weight: 600; border-radius: 4px; padding: 2px 6px; font-size: 0.75rem; display: inline-block; white-space: nowrap;`,
  });
}

export async function render() {
  const data = await all({
    watchlist: get("/api/watchlist"),
    positions: get("/api/positions"),
    universe: get("/api/universe"),
    // Everything the tool can price, with metrics for performance comparisons
    everything: get("/api/watchlist", { all: true, metrics: true }),
  });

  const view = el(
    "div",
    el("h1", { text: "Watchlist & Risk Rankings" }),
    el("p.sub", {
      text: "Track instruments, compare risk groups (1-7 scale), and identify top-performing tickers.",
    }),
  );

  view.append(addSection());
  if (!isError(data.universe)) view.append(universeSection(data.universe));

  const entries = isError(data.watchlist) ? [] : data.watchlist.watchlist || [];
  const everything = isError(data.everything) ? [] : data.everything.watchlist || [];
  const holdings = isError(data.positions) ? [] : data.positions.positions || [];

  if (holdings.length || everything.length) {
    view.append(comparisonChart(everything, holdings));
  }
  view.append(watchTable(entries, isError(data.watchlist) ? null : data.watchlist.basis));
  if (everything.length) {
    view.append(leaderboardSection(addToComparison));
    view.append(catalogueSection(everything));
    view.append(stockBargainFinderSection(addToComparison));
  }

  // Load heavier peer scan and suggestions asynchronously in background so page renders in ms
  const peerSlot = el("div");
  view.append(peerSlot);

  queueMicrotask(async () => {
    try {
      const extra = await all({
        peers: get("/api/peers"),
        suggested: get("/api/instruments/categories/suggested"),
      });
      const buckets = (!isError(data.universe) && data.universe.buckets) || Object.keys(BUCKET_LABEL);
      if (!isError(extra.peers)) {
        peerSlot.replaceChildren(peerSection(extra.peers, buckets, extra.suggested, addToComparison));
      }
    } catch {
      // Peer section fails gracefully if not ready
    }
  });

  return view;
}

/* -------------------------------------------------------------- universe */

/*
 * The pool a peer scan looks in.
 *
 * The scan was built and shipped with nothing to scan: a candidate list of
 * zero, which renders as a blank panel that looks like a broken feature. This
 * is the button that fills it.
 *
 * Loading is a decision, not a default. It is two hundred more symbols in
 * every daily price fetch -- somebody else's rate limit, and minutes of
 * somebody's evening -- so the tool asks. And it says twice, because it is
 * the question people actually have: none of this is owned, and none of it
 * moves a single figure about the portfolio.
 */
function universeSection(body) {
  const loaded = body.loaded;
  const waiting = loaded && body.priced < body.present;

  const act = async (method) => {
    try {
      await (method === "POST" ? post("/api/universe", {}) : del("/api/universe"));
      router.refresh();
    } catch (err) {
      window.alert(err.message);
    }
  };

  const panel = el(
    "div.panel",
    el("h3", { text: "Comparison set" }),
    el("p.note", { text: body.what_this_is }),
    el(
      "p",
      loaded
        ? `${body.present} instruments loaded, ${body.priced} with prices.`
        : `Not loaded. ${body.catalogue_size} well-known shares and funds are ` +
          "available: the Nasdaq-100, the German large caps, and the biggest " +
          "European ETFs.",
    ),
  );

  if (waiting) {
    panel.append(
      el("p.note.warn", {
        text:
          `${body.present - body.priced} of them have no price history yet. ` +
          "They arrive with the next update — until then a peer scan has " +
          "little to measure. Run one from Settings if you do not want to wait.",
      }),
    );
  }

  panel.append(
    el(
      "div.actions",
      loaded
        ? el("button", { type: "button", text: "Remove them", onclick: () => act("DELETE") })
        : el("button.primary", {
            type: "button",
            text: `Load ${body.catalogue_size} instruments`,
            onclick: () => act("POST"),
          }),
    ),
  );

  return el("section", heading("Where comparisons come from"), panel);
}

/* ---------------------------------------------------------------- adding */

function addSection() {
  // Not IWDA.DE. That fund does not trade on Xetra -- it is IWDA.AS in
  // Amsterdam and EUNL.DE here -- so the form was proposing a ticker the feed
  // rejects, which is a poor first impression of a search box.
  const symbol = el("input", { type: "text", placeholder: "EUNL.DE", autocapitalize: "characters" });
  const result = el("div");
  const checkButton = el("button.primary", { type: "submit", text: "Check" });

  const check = async () => {
    const ticker = symbol.value.trim();
    if (!ticker) return;
    checkButton.disabled = true;
    checkButton.textContent = "Checking…";
    result.replaceChildren(el("p.note", { text: "Asking the price feed…" }));
    try {
      const found = await get("/api/watchlist/lookup", { symbol: ticker });
      result.replaceChildren(found.found ? confirmForm(found) : notFound(found, symbol, check));
    } catch (err) {
      result.replaceChildren(el("p.note.bad", { text: err.message }));
    }
    checkButton.disabled = false;
    checkButton.textContent = "Check";
  };

  return el(
    "section",
    heading("Add an instrument"),
    el(
      "form.row",
      { onsubmit: (e) => (e.preventDefault(), check()) },
      el(
        "label.field",
        el("span", { text: "Ticker" }),
        symbol,
        el("span.note", { text: "as the price feed names it — Xetra listings usually end .DE" }),
      ),
      checkButton,
    ),
    result,
  );
}

/*
 * What the feed said, editable before anything is stored. The person knows
 * what they typed; the feed is guessing, and the currency in particular has
 * to be right or every later fetch is rejected.
 */
/** What to offer when the feed has never heard of the ticker. */
function notFound(found, input, recheck) {
  const box = el("div", el("p.note.bad", { text: found.problem }));
  const near = found.did_you_mean || [];
  if (near.length) {
    box.append(
      el("p.note", { text: "The comparison set has these, and the feed knows all of them:" }),
      el(
        "div.chips",
        near.map((n) =>
          el(
            "span.chip",
            el("button.linklike", {
              type: "button",
              text: n.symbol,
              title: `${n.name} (${n.currency}) — check this one instead.`,
              onclick: () => {
                input.value = n.symbol;
                recheck();
              },
            }),
            el("span.muted", { text: n.name }),
          ),
        ),
      ),
    );
  }
  return box;
}

function confirmForm(found) {
  const key = el("input", { type: "text", value: found.suggested_key || found.symbol });
  const name = el("input", { type: "text", value: found.name || "" });
  const currency = el("input", { type: "text", value: found.currency || "EUR", maxlength: "3" });
  const ter = el("input", {
    type: "number",
    step: "1",
    min: "0",
    value: found.ter_bps ? String(found.ter_bps) : "",
    placeholder: "e.g. 20",
  });
  const bucket = el("input", {
    type: "text",
    value: found.risk_bucket || "",
    placeholder: "e.g. EQ_GLOBAL_BROAD",
  });
  const riskLevelSelect = el(
    "select",
    {},
    [1, 2, 3, 4, 5, 6, 7].map((lvl) =>
      el("option", {
        value: String(lvl),
        text: `Level ${lvl} (${RISK_LEVEL_CONFIG[lvl]?.label || ""})`,
        selected: lvl === (found.risk_level || 4),
      }),
    ),
  );
  const status = el("p.note");

  const save = async () => {
    status.className = "note";
    status.textContent = "Adding, and fetching its history…";
    try {
      await post("/api/watchlist", {
        import_key: key.value.trim(),
        name: name.value.trim() || key.value.trim(),
        currency: currency.value.trim().toUpperCase(),
        price_symbol: found.symbol,
        asset_class: found.quote_type === "EQUITY" ? "STOCK" : "ETF",
        ter_bps: ter.value ? Number(ter.value) : null,
        risk_bucket: bucket.value.trim() || null,
        risk_level: riskLevelSelect.value ? Number(riskLevelSelect.value) : null,
        isin: found.isin || null,
      });
      router.refresh();
    } catch (err) {
      status.className = "note bad";
      status.textContent = err.message;
    }
  };

  return el(
    "div.panel",
    el("p", {
      text:
        `${found.symbol}: ${found.observations} recent prices, last ` +
        `${f.qty(found.last_close)} ${found.currency || ""} on ${f.date(found.last_date)}.`,
    }),
    el(
      "form.entry",
      { onsubmit: (e) => (e.preventDefault(), save()) },
      field("Short key", key, "what an import would match on"),
      field("Name", name),
      field("Currency", currency),
      field("Ongoing charge (bps)", ter, "from the fund's key information document"),
      field("Risk category", bucket, "e.g. EQ_SECTOR, EQ_GLOBAL_BROAD"),
      field("Risk level (1-7)", riskLevelSelect, "1 = Lowest, 7 = Highest"),
      el("div.wide", el("button.primary", { type: "submit", text: "Add to watchlist" })),
    ),
    status,
    el("p.note", { text: found.note }),
  );
}

function field(label, node, hint = null) {
  return el(
    "label.field",
    el("span", { text: label }),
    node,
    hint ? el("span.note", { text: hint }) : null,
  );
}

/* ----------------------------------------------------------------- table */

function watchTable(entries, basis) {
  if (!entries.length) {
    return el(
      "div.empty",
      el("p", { text: "Nothing on the watchlist yet." }),
      el("p", {
        text:
          "Add an instrument above and it will be priced alongside your holdings, " +
          "then compared with them.",
      }),
    );
  }

  const columns = [
    {
      label: "Ticker",
      key: "instrument",
      type: "str",
      render: (e) => ({ node: el("strong", { text: e.instrument }), cls: "name" }),
    },
    {
      label: "Risk",
      key: (e) => e.risk_level || 0,
      type: "num",
      render: (e) => ({ node: riskBadge(e.risk_level) }),
    },
    {
      label: "Name",
      key: "name",
      type: "str",
      render: (e) => ({ text: e.name, cls: "name muted" }),
    },
    {
      label: "1 year",
      key: (e) => f.num(e.performance?.ret_1y),
      type: "pct",
      defaultSort: true,
      defaultDir: "desc",
      render: (e) => {
        const p = e.performance || {};
        return { text: f.pct(p.ret_1y, { sign: true }), cls: f.tone(p.ret_1y) };
      },
    },
    {
      label: "3 years a year",
      key: (e) => f.num(e.performance?.ret_3y_ann),
      type: "pct",
      render: (e) => {
        const p = e.performance || {};
        return { text: f.pct(p.ret_3y_ann, { sign: true }), cls: f.tone(p.ret_3y_ann) };
      },
    },
    {
      label: "Volatility",
      key: (e) => f.num(e.performance?.vol_1y),
      type: "pct",
      render: (e) => ({ text: f.pct(e.performance?.vol_1y) }),
    },
    {
      label: "Worst fall",
      key: (e) => f.num(e.performance?.max_dd_3y),
      type: "pct",
      render: (e) => {
        const p = e.performance || {};
        return { text: f.pct(p.max_dd_3y), cls: p.max_dd_3y ? "loss" : "" };
      },
    },
    {
      label: "Fee",
      key: (e) => e.ter_bps ?? -1,
      type: "num",
      render: (e) => ({ node: terCell(e) }),
    },
    {
      label: "",
      sortable: false,
      render: (e) => {
        const p = e.performance || {};
        return {
          node: el(
            "span",
            { style: "display: flex; gap: 0.35rem; align-items: center; justify-content: flex-end;" },
            e.prices === 0 ? el("span.tag.warn", { text: "no prices yet" }) : null,
            p.basis === "PRICE"
              ? el("span.tag.warn", { text: "price only", title: (p.caveats || [])[0] })
              : null,
            el("button.small", {
              type: "button",
              text: "Chart",
              title: `Add ${e.instrument} to the comparison chart above.`,
              onclick: () => addToComparison(e.instrument),
            }),
            buyButton(e),
            removeButton(e),
          ),
        };
      },
    },
  ];

  const tableNode = sortableTable(columns, entries, {
    placeholder: "Search watched tickers, names…",
    searchKeys: ["instrument", "name", "risk_bucket"],
  });

  return el(
    "section",
    heading("Being watched"),
    tableNode,
    basis ? el("p.note", { text: basis }) : null,
  );
}

/** The fee is typed, not fetched: no free source supplies a UCITS charge. */
function terCell(entry) {
  if (entry.ter_bps !== null && entry.ter_bps !== undefined) {
    return el("span", { text: f.pct(entry.ter_pct) });
  }
  const input = el("input", {
    type: "number",
    step: "1",
    min: "0",
    placeholder: "bps",
    style: "width:5rem",
  });
  const save = async () => {
    if (!input.value) return;
    try {
      await patch(`/api/instruments/${encodeURIComponent(entry.instrument)}/details`, {
        ter_bps: Number(input.value),
      });
      router.refresh();
    } catch (err) {
      window.alert(err.message);
    }
  };
  input.addEventListener("change", save);
  return input;
}

/*
 * Straight into the trade form with this instrument already chosen.
 *
 * Watching something and then buying it were two disconnected screens: the
 * only way to record a purchase of a watched instrument was to import a trade
 * in it from elsewhere, which made the watchlist a place things went to be
 * looked at and never acted on.
 */
function buyButton(entry) {
  return el("a.button.small", {
    href: `#/trades?type=BUY&instrument=${encodeURIComponent(entry.instrument)}`,
    text: "Buy",
    title: `Record a purchase of ${entry.instrument}.`,
  });
}

function removeButton(entry) {
  return el("button", {
    text: "Remove",
    onclick: async () => {
      if (!window.confirm(`Stop watching ${entry.instrument}? Its prices are kept.`)) return;
      try {
        await del(`/api/watchlist/${encodeURIComponent(entry.instrument)}`);
        router.refresh();
      } catch (err) {
        window.alert(err.message);
      }
    },
  });
}

/* ----------------------------------------------------------------- chart */

/*
 * A chart you choose the lines for.
 *
 * It used to pick up to six instruments on your behalf -- every watched one,
 * then the three largest holdings -- which is the one selection nobody wants:
 * too many to read, and never the comparison you had in mind. Now it starts
 * with your holdings and takes anything else you add, including a ticker
 * clicked in the tables below.
 */
const compared = new Set();
let redrawComparison = () => {};

function addToComparison(key) {
  compared.add(key);
  redrawComparison();
  document.getElementById("compare-chart")?.scrollIntoView({ behavior: "smooth", block: "center" });
}

/*
 * What is actually in the comparison set.
 *
 * "There is no place for me to see the tickers" was exactly right: the panel
 * said "243 loaded" and named none of them, which is a number to take on
 * trust rather than a list to check. Collapsed by default because it is two
 * hundred rows, and grouped by category because that is what decides which
 * of them can be a peer for which of your holdings.
 */
function catalogueSection(everything) {
  const pool = everything.filter((e) => e.status === "UNIVERSE");
  if (!pool.length) return el("div");

  const tableContainer = el("div");

  // Slider filter state
  let maxFeeBps = null;
  let min1yPct = null;
  let maxRisk = null;
  let maxVolPct = null;
  let minSharpe = null;

  const columns = [
    {
      label: "Ticker",
      key: "instrument",
      type: "str",
      defaultSort: true,
      defaultDir: "asc",
      render: (e) => ({ node: el("strong", { text: e.instrument }), cls: "name" }),
    },
    {
      label: "Risk",
      key: (e) => e.risk_level || 0,
      type: "num",
      render: (e) => ({ node: riskBadge(e.risk_level) }),
    },
    {
      label: "Name",
      key: "name",
      type: "str",
      render: (e) => ({ text: e.name, cls: "name muted" }),
    },
    {
      label: "Category",
      key: (e) => BUCKET_LABEL[e.risk_bucket] || e.risk_bucket || "Uncategorised",
      type: "str",
      render: (e) => ({ text: BUCKET_LABEL[e.risk_bucket] || e.risk_bucket || "—" }),
    },
    {
      label: "Fee",
      key: (e) => e.ter_bps ?? -1,
      type: "num",
      render: (e) => ({
        text: e.ter_pct !== null && e.ter_pct !== undefined ? f.pct(e.ter_pct) : (e.ter_bps ? f.bps(e.ter_bps) : "—"),
      }),
    },
    {
      label: "3M",
      tooltip: "3-month total return",
      key: (e) => f.num(e.performance?.ret_3m),
      type: "pct",
      render: (e) => ({
        text: f.pct(e.performance?.ret_3m, { sign: true }),
        cls: f.tone(e.performance?.ret_3m),
      }),
    },
    {
      label: "6M",
      tooltip: "6-month total return",
      key: (e) => f.num(e.performance?.ret_6m),
      type: "pct",
      render: (e) => ({
        text: f.pct(e.performance?.ret_6m, { sign: true }),
        cls: f.tone(e.performance?.ret_6m),
      }),
    },
    {
      label: "1Y",
      tooltip: "1-year total return",
      key: (e) => f.num(e.performance?.ret_1y),
      type: "pct",
      render: (e) => ({
        text: f.pct(e.performance?.ret_1y, { sign: true }),
        cls: f.tone(e.performance?.ret_1y),
      }),
    },
    {
      label: "Volatility",
      tooltip: "Annualized standard deviation of returns (1-year or 3-year)",
      key: (e) => f.num(e.performance?.vol_1y ?? e.performance?.vol_3y),
      type: "pct",
      render: (e) => {
        const v = e.performance?.vol_1y ?? e.performance?.vol_3y;
        return { text: v !== null && v !== undefined ? f.pct(v) : "—" };
      },
    },
    {
      label: "Sharpe",
      tooltip: "Sharpe ratio: excess return over risk-free rate divided by volatility. Higher indicates better risk-adjusted return.",
      key: (e) => f.num(e.performance?.sharpe_3y ?? e.performance?.sharpe_1y),
      type: "num",
      render: (e) => {
        const s = e.performance?.sharpe_3y ?? e.performance?.sharpe_1y;
        return {
          text: s !== null && s !== undefined ? Number(s).toFixed(2) : "—",
          cls: s && Number(s) > 0 ? "gain" : "",
        };
      },
    },
    {
      label: "Prices",
      key: (e) => e.prices || 0,
      type: "num",
      render: (e) => ({ text: e.prices ? `${e.prices} obs` : "none yet" }),
    },
    {
      label: "",
      sortable: false,
      render: (e) => ({
        node: el(
          "span",
          { style: "display: inline-flex; gap: 0.35rem;" },
          el("button.small", {
            type: "button",
            text: "Chart",
            title: `Add ${e.instrument} to the comparison chart above.`,
            onclick: () => addToComparison(e.instrument),
          }),
          el("button.small", {
            type: "button",
            text: "Watch",
            title: `Move ${e.instrument} to your active watchlist.`,
            onclick: async () => {
              try {
                await post("/api/watchlist", {
                  import_key: e.instrument,
                  name: e.name,
                  currency: e.currency,
                  price_symbol: e.price_symbol,
                  asset_class: e.asset_class,
                  ter_bps: e.ter_bps,
                  risk_bucket: e.risk_bucket,
                  risk_level: e.risk_level,
                });
                router.refresh();
              } catch (err) {
                window.alert(err.message);
              }
            },
          }),
        ),
      }),
    },
  ];

  const renderFilteredTable = () => {
    const filtered = pool.filter((e) => {
      if (maxFeeBps !== null && (e.ter_bps === null || e.ter_bps === undefined || e.ter_bps > maxFeeBps)) {
        return false;
      }
      if (min1yPct !== null) {
        const ret1y = e.performance?.ret_1y;
        if (ret1y === null || ret1y === undefined || Number(ret1y) < min1yPct / 100) {
          return false;
        }
      }
      if (maxRisk !== null && ((e.risk_level || 4) > maxRisk)) {
        return false;
      }
      if (maxVolPct !== null) {
        const v = e.performance?.vol_1y ?? e.performance?.vol_3y;
        if (v === null || v === undefined || Number(v) > maxVolPct / 100) {
          return false;
        }
      }
      if (minSharpe !== null) {
        const s = e.performance?.sharpe_3y ?? e.performance?.sharpe_1y;
        if (s === null || s === undefined || Number(s) < minSharpe) {
          return false;
        }
      }
      return true;
    });

    const tableNode = sortableTable(columns, filtered, {
      placeholder: "Search comparison universe tickers, categories, names…",
      searchKeys: ["instrument", "name", "risk_bucket"],
      pageSize: 20,
      allowShowAll: true,
    });
    tableContainer.replaceChildren(tableNode);
  };

  // Create the slider controls
  const feeBadge = el("span.slider-filter-badge", { text: "Any Fee" });
  const feeSlider = el("input.slider-range-input", {
    type: "range",
    min: "0",
    max: "100",
    step: "5",
    value: "100",
  });
  feeSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val >= 100) {
      maxFeeBps = null;
      feeBadge.textContent = "Any Fee";
    } else {
      maxFeeBps = val;
      feeBadge.textContent = `≤ ${(val / 100).toFixed(2)}% (${val} bps)`;
    }
    renderFilteredTable();
  });

  const ret1yBadge = el("span.slider-filter-badge", { text: "Any Return" });
  const ret1ySlider = el("input.slider-range-input", {
    type: "range",
    min: "-30",
    max: "50",
    step: "5",
    value: "-30",
  });
  ret1ySlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val <= -30) {
      min1yPct = null;
      ret1yBadge.textContent = "Any Return";
    } else {
      min1yPct = val;
      ret1yBadge.textContent = `≥ ${val > 0 ? "+" : ""}${val}%`;
    }
    renderFilteredTable();
  });

  const riskBadgeEl = el("span.slider-filter-badge", { text: "All Levels (1–7)" });
  const riskSlider = el("input.slider-range-input", {
    type: "range",
    min: "1",
    max: "7",
    step: "1",
    value: "7",
  });
  riskSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val >= 7) {
      maxRisk = null;
      riskBadgeEl.textContent = "All Levels (1–7)";
    } else {
      maxRisk = val;
      riskBadgeEl.textContent = `≤ Level ${val}/7`;
    }
    renderFilteredTable();
  });

  const volBadge = el("span.slider-filter-badge", { text: "Any Volatility" });
  const volSlider = el("input.slider-range-input", {
    type: "range",
    min: "5",
    max: "45",
    step: "5",
    value: "45",
  });
  volSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val >= 45) {
      maxVolPct = null;
      volBadge.textContent = "Any Volatility";
    } else {
      maxVolPct = val;
      volBadge.textContent = `≤ ${val}%`;
    }
    renderFilteredTable();
  });

  const sharpeBadge = el("span.slider-filter-badge", { text: "Any Sharpe" });
  const sharpeSlider = el("input.slider-range-input", {
    type: "range",
    min: "-0.5",
    max: "2.0",
    step: "0.25",
    value: "-0.5",
  });
  sharpeSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val <= -0.5) {
      minSharpe = null;
      sharpeBadge.textContent = "Any Sharpe";
    } else {
      minSharpe = val;
      sharpeBadge.textContent = `≥ ${val.toFixed(2)}`;
    }
    renderFilteredTable();
  });

  const resetBtn = el("button.small", {
    type: "button",
    text: "Reset Sliders",
    style: "align-self: flex-end; margin-top: auto;",
    onclick: () => {
      maxFeeBps = null;
      min1yPct = null;
      maxRisk = null;
      maxVolPct = null;
      minSharpe = null;
      feeSlider.value = "100";
      feeBadge.textContent = "Any Fee";
      ret1ySlider.value = "-30";
      ret1yBadge.textContent = "Any Return";
      riskSlider.value = "7";
      riskBadgeEl.textContent = "All Levels (1–7)";
      volSlider.value = "45";
      volBadge.textContent = "Any Volatility";
      sharpeSlider.value = "-0.5";
      sharpeBadge.textContent = "Any Sharpe";
      renderFilteredTable();
    },
  });

  const sliderPanel = el(
    "div.slider-filter-panel",
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Max Ongoing Charge" }), feeBadge),
      feeSlider,
      el("div.slider-range-labels", el("span", { text: "0%" }), el("span", { text: "0.50%" }), el("span", { text: "Any (>1.0%)" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Min 1-Year Return" }), ret1yBadge),
      ret1ySlider,
      el("div.slider-range-labels", el("span", { text: "Any" }), el("span", { text: "+10%" }), el("span", { text: "+50%" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Max Risk Tier (SRI 1–7)" }), riskBadgeEl),
      riskSlider,
      el("div.slider-range-labels", el("span", { text: "1 (Low)" }), el("span", { text: "4 (Moderate)" }), el("span", { text: "7 (All)" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Max Volatility" }), volBadge),
      volSlider,
      el("div.slider-range-labels", el("span", { text: "5%" }), el("span", { text: "25%" }), el("span", { text: "Any (≥45%)" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Min Sharpe Ratio" }), sharpeBadge),
      sharpeSlider,
      el("div.slider-range-labels", el("span", { text: "Any (<0)" }), el("span", { text: "0.75" }), el("span", { text: "+2.0" })),
    ),
    resetBtn,
  );

  renderFilteredTable();

  return el(
    "section",
    heading("What is in the comparison set"),
    el(
      "details.assumptions",
      { open: true },
      el("summary", { text: `${pool.length} instruments in comparison catalogue` }),
      sliderPanel,
      tableContainer,
    ),
  );
}

const COMPARE_RANGES = [
  ["1M", "1 month", 31],
  ["3M", "3 months", 92],
  ["6M", "6 months", 183],
  ["YTD", "This year", null],
  ["1Y", "1 year", 366],
  ["3Y", "3 years", 1096],
  ["ALL", "All", null],
];
let compareRange = "1Y";

function comparisonChart(entries, holdings) {
  const pool = [
    ...holdings.map((h) => ({ key: h.instrument, name: h.name, held: true })),
    ...entries.filter((e) => e.prices).map((e) => ({ key: e.instrument, name: e.name })),
  ];
  if (!pool.length) return el("div");

  if (!compared.size && holdings.length) compared.add(holdings[0].instrument);

  const box = el("div.chart", { id: "compare-chart" });
  const chosen = el("div.chips");
  const rangeBox = el("div.ranges", { style: "margin: 0.5rem 0 0.8rem;" });

  const draw = async () => {
    const keys = [...compared];
    const chipNodes = keys.map((k) =>
      el(
        "span.chip",
        el("span", { text: k }),
        el("button.linklike", {
          type: "button",
          text: "×",
          title: `Take ${k} off the chart.`,
          onclick: () => {
            compared.delete(k);
            draw();
          },
        }),
      ),
    );

    if (!keys.length) {
      chosen.replaceChildren(el("span.muted", { text: "No instruments selected. Pick one above or click 'Chart' on any ticker." }));
      box.replaceChildren();
      return;
    }
    chosen.replaceChildren(...chipNodes);

    const pal = palette();
    const loaded = await all(
      Object.fromEntries(
        keys.map((k) => [k, get(`/api/instruments/${encodeURIComponent(k)}/series`)]),
      ),
    );

    // Compute window cutoff date for rebasing
    let cutoffStr = null;
    const today = new Date();
    const rangeOpt = COMPARE_RANGES.find(([k]) => k === compareRange);
    if (compareRange === "YTD") {
      cutoffStr = `${today.getFullYear()}-01-01`;
    } else if (rangeOpt && rangeOpt[2]) {
      const d = new Date(today.getTime() - rangeOpt[2] * 86400000);
      cutoffStr = d.toISOString().slice(0, 10);
    }

    const rawDates = new Set();
    const seriesByInstrument = {};

    for (const k of keys) {
      if (!isError(loaded[k])) {
        const raw = loaded[k].points || [];
        seriesByInstrument[k] = raw;
        for (const pt of raw) rawDates.add(pt.date);
      }
    }

    const allDatesSorted = [...rawDates].sort();
    if (allDatesSorted.length < 2) return;

    // Filter keys that actually have price points
    const validKeys = keys.filter(
      (k) => seriesByInstrument[k] && seriesByInstrument[k].length > 0,
    );

    // When comparing multiple tickers, "All" filter and timeframes longer than
    // the date a price history is available for one of the compared items must
    // default to the earliest date where price history is available for all items.
    let effectiveCutoff = cutoffStr;
    if (validKeys.length > 1) {
      const startDates = validKeys.map((k) => seriesByInstrument[k][0].date);
      const commonStartDate = startDates.reduce((max, d) => (d > max ? d : max), startDates[0]);
      if (compareRange === "ALL" || !effectiveCutoff || effectiveCutoff < commonStartDate) {
        effectiveCutoff = commonStartDate;
      }
    }

    let axis = allDatesSorted;
    if (effectiveCutoff) {
      const idx = axis.findIndex((d) => d >= effectiveCutoff);
      if (idx !== -1) axis = axis.slice(idx);
    }
    if (axis.length < 2) axis = allDatesSorted;

    const series = [];
    keys.forEach((k, i) => {
      const raw = seriesByInstrument[k];
      if (!raw || !raw.length) return;
      const rawMap = new Map(raw.map((pt) => [pt.date, pt.level]));
      const firstRawDate = raw[0].date;

      // Initialize with the most recent price on or before axis[0]
      let lastVal = null;
      for (let j = raw.length - 1; j >= 0; j--) {
        if (raw[j].date <= axis[0]) {
          lastVal = raw[j].level;
          break;
        }
      }

      // Forward fill across axis so weekly and daily lines plot continuously
      const filled = axis.map((d) => {
        if (d < firstRawDate && lastVal === null) return null;
        const current = rawMap.get(d);
        if (current !== null && current !== undefined && Number.isFinite(current)) {
          lastVal = current;
          return current;
        }
        return lastVal;
      });

      const indexedData = indexTo100(filled);
      if (indexedData.some((v) => v !== null)) {
        const longName = loaded[k]?.name || pool.find((p) => p.key === k)?.name || k;
        series.push({
          label: longName,
          data: indexedData,
          colour: pal.series[i % pal.series.length],
          width: holdings.some((h) => h.instrument === k) ? 2.4 : 1.8,
          dash: holdings.some((h) => h.instrument === k) ? undefined : [4, 3],
          spanGaps: true,
        });
      }
    });

    if (series.length) {
      timeChart(box, { x: axis.map(toTs), height: 320, kind: "index", series, baseline: 100 });
    }
  };
  redrawComparison = draw;

  const updateRangeButtons = () => {
    rangeBox.replaceChildren(
      ...COMPARE_RANGES.map(([key, label]) =>
        el("button", {
          text: label,
          "aria-pressed": String(key === compareRange),
          onclick: () => {
            compareRange = key;
            updateRangeButtons();
            draw();
          },
        }),
      ),
    );
  };
  updateRangeButtons();

  const option = (x) => el("option", { value: x.key, text: `${x.key} — ${x.name}` });
  const held = pool.filter((x) => x.held);
  const rest = pool.filter((x) => !x.held);
  const picker = el(
    "select",
    {
      onchange: (e) => {
        if (e.target.value) addToComparison(e.target.value);
        e.target.value = "";
      },
    },
    el("option", { value: "", text: "Add an instrument to compare…" }),
    held.length ? el("optgroup", { label: "You hold" }, held.map(option)) : null,
    rest.length
      ? el("optgroup", { label: "Watched and comparison set" }, rest.map(option))
      : null,
  );

  queueMicrotask(draw);

  return el(
    "section",
    heading("Compare anything against anything"),
    el(
      "div.panel",
      el("div.actions", picker),
      chosen,
      rangeBox,
      box,
      el("p.chart-note", {
        text:
          "All indexed to 100 at the start of the selected window, in euros. Solid line is held, dashed is watched. Red dashed line marks baseline.",
      }),
    ),
  );
}

/*
 * A category picker for one holding.
 *
 * The panel used to name what was missing and then say "set one on each
 * holding" -- with no field anywhere in the app that could set it. The whole
 * peer feature was unreachable behind a sentence of advice.
 *
 * A guess is not offered as a default. `core/peers.py` refuses to compare on
 * a derived category precisely because it cannot tell a semiconductor fund
 * from a world tracker, and prefilling the box with that guess would hand the
 * refusal straight back as a click-through -- so the proposal comes from the
 * fund's own name instead, with the words it matched shown beside it.
 */
function leaderboardSection(onCompare) {
  const container = el("div");
  let activeLevel = 4; // Default to Core Global Equity (Level 4)
  let currentCandidates = [];

  // Slider filter state for leaderboard
  let min1yPct = null;
  let maxVolPct = null;
  let minSharpe = null;

  const columns = [
    {
      label: "Rank & Ticker",
      key: "instrument",
      type: "str",
      render: (c) => ({
        node: el(
          "span",
          el("strong", { text: `#${c._origRank}` }),
          " ",
          el("button.linklike", {
            type: "button",
            text: c.instrument,
            title: `Add ${c.instrument} to comparison chart.`,
            onclick: () => onCompare && onCompare(c.instrument),
          }),
          " ",
          el("span.muted", { text: c.name }),
        ),
        cls: "name",
      }),
    },
    {
      label: "1M",
      tooltip: "1-month total return",
      key: (c) => f.num(c.ret_1m),
      type: "pct",
      render: (c) => ({ text: f.pct(c.ret_1m, { sign: true }), cls: f.tone(c.ret_1m) }),
    },
    {
      label: "3M",
      tooltip: "3-month total return",
      key: (c) => f.num(c.ret_3m),
      type: "pct",
      render: (c) => ({ text: f.pct(c.ret_3m, { sign: true }), cls: f.tone(c.ret_3m) }),
    },
    {
      label: "6M",
      tooltip: "6-month total return",
      key: (c) => f.num(c.ret_6m),
      type: "pct",
      render: (c) => ({ text: f.pct(c.ret_6m, { sign: true }), cls: f.tone(c.ret_6m) }),
    },
    {
      label: "1 Year",
      tooltip: "1-year total return",
      key: (c) => f.num(c.ret_1y),
      type: "pct",
      render: (c) => ({ text: f.pct(c.ret_1y, { sign: true }), cls: f.tone(c.ret_1y) }),
    },
    {
      label: "3Y Ann.",
      tooltip: "3-year annualized total return",
      key: (c) => f.num(c.ret_3y_ann),
      type: "pct",
      render: (c) => ({ text: f.pct(c.ret_3y_ann, { sign: true }), cls: f.tone(c.ret_3y_ann) }),
    },
    {
      label: "Volatility",
      tooltip: "Annualized standard deviation of returns: measures how much the price fluctuates over time. Higher means larger swings and risk.",
      key: (c) => f.num(c.vol_1y ?? c.vol_3y),
      type: "pct",
      render: (c) => ({
        text: f.pct(c.vol_1y ?? c.vol_3y),
        title: "Annualized standard deviation: higher values reflect wider price swings.",
      }),
    },
    {
      label: "Sharpe",
      tooltip: "Sharpe ratio: excess return over the risk-free rate divided by volatility. Measures return efficiency per unit of risk (>1 is good, >2 is excellent).",
      key: (c) => f.num(c.sharpe_3y ?? c.sharpe_1y),
      type: "num",
      defaultSort: true,
      defaultDir: "desc",
      render: (c) => {
        const sharpeVal = c.sharpe_3y ?? c.sharpe_1y;
        return {
          text:
            sharpeVal !== null && sharpeVal !== undefined ? Number(sharpeVal).toFixed(2) : "—",
          cls: sharpeVal && Number(sharpeVal) > 0 ? "gain" : "",
          title: "Sharpe ratio: excess return over risk-free rate divided by volatility. Higher is better.",
        };
      },
    },
    {
      label: "Fee",
      tooltip: "Ongoing charge (TER) in basis points: 20 bps = 0.20% per year",
      key: (c) => c.ter_bps ?? -1,
      type: "num",
      render: (c) => ({
        text: c.ter_bps === null || c.ter_bps === undefined ? "—" : f.bps(c.ter_bps),
      }),
    },
    {
      label: "",
      sortable: false,
      render: (c) => ({
        node: el("button.small", {
          type: "button",
          text: "Chart",
          onclick: () => onCompare && onCompare(c.instrument),
        }),
      }),
    },
  ];

  const renderFilteredCandidates = () => {
    if (!currentCandidates.length) {
      container.replaceChildren(
        el("p.note", { text: `No priced instruments found for Risk Level ${activeLevel}.` }),
      );
      return;
    }

    const filtered = currentCandidates.filter((c) => {
      if (min1yPct !== null) {
        if (c.ret_1y === null || c.ret_1y === undefined || Number(c.ret_1y) < min1yPct / 100) {
          return false;
        }
      }
      if (maxVolPct !== null) {
        const v = c.vol_1y ?? c.vol_3y;
        if (v === null || v === undefined || Number(v) > maxVolPct / 100) {
          return false;
        }
      }
      if (minSharpe !== null) {
        const s = c.sharpe_3y ?? c.sharpe_1y;
        if (s === null || s === undefined || Number(s) < minSharpe) {
          return false;
        }
      }
      return true;
    });

    if (!filtered.length) {
      container.replaceChildren(
        el(
          "div.empty",
          el("p", {
            text: `No instruments match the selected return, volatility, or Sharpe filters for Risk Level ${activeLevel}.`,
          }),
        ),
      );
      return;
    }

    const tableNode = sortableTable(columns, filtered, {
      placeholder: `Search Level ${activeLevel} performers…`,
      searchKeys: ["instrument", "name", "bucket"],
      pageSize: 10,
      allowShowAll: true,
    });
    container.replaceChildren(tableNode);
  };

  const loadData = async (lvl) => {
    container.replaceChildren(el("p.note", { text: "Loading top performers in risk group…" }));
    try {
      const res = await get("/api/peers/leaderboard", { risk_level: lvl, top: 500 });
      currentCandidates = res.candidates || [];
      currentCandidates.forEach((c, idx) => {
        c._origRank = idx + 1;
      });
      renderFilteredCandidates();
    } catch (err) {
      container.replaceChildren(el("p.note.bad", { text: err.message }));
    }
  };

  // Slider filter controls
  const ret1yBadge = el("span.slider-filter-badge", { text: "Any Return" });
  const ret1ySlider = el("input.slider-range-input", {
    type: "range",
    min: "-30",
    max: "50",
    step: "5",
    value: "-30",
  });
  ret1ySlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val <= -30) {
      min1yPct = null;
      ret1yBadge.textContent = "Any Return";
    } else {
      min1yPct = val;
      ret1yBadge.textContent = `≥ ${val > 0 ? "+" : ""}${val}%`;
    }
    renderFilteredCandidates();
  });

  const volBadge = el("span.slider-filter-badge", { text: "Any Volatility" });
  const volSlider = el("input.slider-range-input", {
    type: "range",
    min: "5",
    max: "45",
    step: "5",
    value: "45",
  });
  volSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val >= 45) {
      maxVolPct = null;
      volBadge.textContent = "Any Volatility";
    } else {
      maxVolPct = val;
      volBadge.textContent = `≤ ${val}%`;
    }
    renderFilteredCandidates();
  });

  const sharpeBadge = el("span.slider-filter-badge", { text: "Any Sharpe" });
  const sharpeSlider = el("input.slider-range-input", {
    type: "range",
    min: "-0.5",
    max: "2.0",
    step: "0.25",
    value: "-0.5",
  });
  sharpeSlider.addEventListener("input", (ev) => {
    const val = Number(ev.target.value);
    if (val <= -0.5) {
      minSharpe = null;
      sharpeBadge.textContent = "Any Sharpe";
    } else {
      minSharpe = val;
      sharpeBadge.textContent = `≥ ${val.toFixed(2)}`;
    }
    renderFilteredCandidates();
  });

  const resetBtn = el("button.small", {
    type: "button",
    text: "Reset Sliders",
    style: "align-self: flex-end; margin-top: auto;",
    onclick: () => {
      min1yPct = null;
      maxVolPct = null;
      minSharpe = null;
      ret1ySlider.value = "-30";
      ret1yBadge.textContent = "Any Return";
      volSlider.value = "45";
      volBadge.textContent = "Any Volatility";
      sharpeSlider.value = "-0.5";
      sharpeBadge.textContent = "Any Sharpe";
      renderFilteredCandidates();
    },
  });

  const sliderPanel = el(
    "div.slider-filter-panel",
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Min 1-Year Return" }), ret1yBadge),
      ret1ySlider,
      el("div.slider-range-labels", el("span", { text: "Any" }), el("span", { text: "+10%" }), el("span", { text: "+50%" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Max Volatility" }), volBadge),
      volSlider,
      el("div.slider-range-labels", el("span", { text: "5%" }), el("span", { text: "25%" }), el("span", { text: "Any (≥45%)" })),
    ),
    el(
      "div.slider-filter-group",
      el("div.slider-filter-header", el("span", { text: "Min Sharpe Ratio" }), sharpeBadge),
      sharpeSlider,
      el("div.slider-range-labels", el("span", { text: "Any (<0)" }), el("span", { text: "0.75" }), el("span", { text: "+2.0" })),
    ),
    resetBtn,
  );

  const nav = el(
    "div.chips",
    { style: "margin-bottom: 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem;" },
    [1, 2, 3, 4, 5, 6, 7].map((lvl) => {
      const cfg = RISK_LEVEL_CONFIG[lvl];
      const btn = el("button.chip", {
        type: "button",
        text: `Level ${lvl}: ${cfg.label.split("·")[1]?.trim() || ""}`,
        style: lvl === activeLevel ? `font-weight: bold; border-color: ${cfg.color}; background: ${cfg.bg};` : "",
        onclick: () => {
          activeLevel = lvl;
          for (const sibling of nav.children) {
            sibling.style.fontWeight = "normal";
            sibling.style.borderColor = "";
            sibling.style.background = "";
          }
          btn.style.fontWeight = "bold";
          btn.style.borderColor = cfg.color;
          btn.style.background = cfg.bg;
          loadData(lvl);
        },
      });
      return btn;
    }),
  );

  loadData(activeLevel);

  return el(
    "section",
    heading("Best performers by risk group"),
    el("p.note", {
      text:
        "Find the top-returning tickers in each numerical risk tier (1-7 SRI scale). " +
        "Ranked by risk-adjusted efficiency (Sharpe ratio and annualized returns). " +
        "Tickers with at least 1 year of history are fully supported.",
    }),
    nav,
    sliderPanel,
    container,
  );
}

function categoryRow(key, buckets, proposal) {
  const select = el(
    "select",
    el("option", { value: "", text: "Choose category…" }),
    buckets.map((b) =>
      el("option", {
        value: b,
        text: BUCKET_LABEL[b] || b,
        selected: proposal ? b === (proposal.current || proposal.suggested) : false,
      }),
    ),
  );
  const levelSelect = el(
    "select",
    { title: "Risk Level (1-7 scale)" },
    [1, 2, 3, 4, 5, 6, 7].map((lvl) =>
      el("option", {
        value: `${lvl}`,
        text: `Risk ${lvl}/7`,
        selected: proposal ? lvl === (proposal.current_risk_level || proposal.suggested_risk_level) : lvl === 4,
      }),
    ),
  );
  const save = el("button.small.primary", {
    type: "button",
    text: "Save",
    onclick: async () => {
      if (!select.value) return;
      save.disabled = true;
      try {
        await patch(`/api/instruments/${encodeURIComponent(key)}/details`, {
          risk_bucket: select.value,
          risk_level: levelSelect.value ? Number(levelSelect.value) : undefined,
        });
        router.refresh();
      } catch (err) {
        window.alert(err.message);
        save.disabled = false;
      }
    },
  });
  return row([
    { text: key, cls: "name" },
    { text: proposal?.name || "", cls: "prose" },
    { node: el("span", select, " ", levelSelect) },
    { text: proposal?.why || "", cls: "prose muted" },
    { node: save },
  ]);
}

/** Accept every proposal at once, then let the page re-read the result. */
function acceptAllButton(proposals) {
  const usable = proposals.filter((x) => x.suggested && !x.current);
  if (!usable.length) return null;
  const button = el("button.primary", {
    type: "button",
    text: `Accept all ${usable.length}`,
    title: "Save every suggested category above, in one go.",
    onclick: async () => {
      button.disabled = true;
      button.textContent = "Saving…";
      try {
        const categories = {};
        const risk_levels = {};
        for (const x of usable) {
          categories[x.instrument] = x.suggested;
          if (x.suggested_risk_level) risk_levels[x.instrument] = x.suggested_risk_level;
        }
        await post("/api/instruments/categories", { categories, risk_levels });
        router.refresh();
      } catch (err) {
        window.alert(err.message);
        button.disabled = false;
        button.textContent = `Accept all ${usable.length}`;
      }
    },
  });
  return button;
}

function peerSection(body, buckets, suggested, onCompare) {
  const scans = body.scans || [];
  const needs = body.needs_category || [];
  const proposals = isError(suggested) ? [] : suggested?.suggestions || [];
  const byKey = new Map(proposals.map((p) => [p.instrument, p]));
  const section = el("section", heading("Comparable to what you hold"));

  if (scans.length) {
    section.append(
      explainPanel(
        "/api/peers",
        {},
        "Write the whole comparison up in sentences",
      ),
    );
  }

  for (const scan of scans) section.append(scanPanel(scan, onCompare));
  if (scans.length) section.append(el("p.note", { text: scans[0].disclaimer }));
  else section.append(el("p.note", { text: body.note }));

  if (needs.length || proposals.length) {
    section.append(
      el(
        needs.length ? "div.panel.warn" : "div.panel",
        el("h3", { text: needs.length ? "Not compared yet" : "Risk categories" }),
        el("p", {
          text: needs.length
            ? `${needs.join(", ")} have no risk category recorded, so there is nothing ` +
              "to say they are like. Comparing them against a broad tracker would " +
              "produce a precise number about two unlike things."
            : "What each holding is treated as when looking for alternatives. " +
              "Change any of them and the comparisons below are redrawn.",
        }),
        el("p.note", {
          text:
            proposals.length
              ? "Each one is already filled in from the fund's own name, with the " +
                "reason beside it. Read the reason, change anything that looks " +
                "wrong, then save."
              : "Pick what each one actually is. Nothing else on this page " +
                "changes; it only decides which instruments are treated as its peers.",
        }),
        el(
          "div.wrap",
          el(
            "table",
            el(
              "thead",
              el(
                "tr",
                ["Holding", "Name", "Category & Risk", "Why", ""].map((h) => el("th", { text: h })),
              ),
            ),
            el(
              "tbody",
              (needs.length ? needs : proposals.map((x) => x.instrument)).map((key) =>
                categoryRow(key, buckets, byKey.get(key)),
              ),
            ),
          ),
        ),
        el("div.actions", acceptAllButton(proposals)),
      ),
    );
  }
  return section;
}

export function valueScoreBadge(score, badge) {
  if (!badge) return el("span.tag", { text: score != null ? `${score}/100` : "—" });
  let cls = "badge-fair";
  let label = `${badge} · ${score}/100`;

  if (badge === "BARGAIN") {
    cls = "badge-bargain";
    label = `🏷️ Bargain · ${score}/100`;
  } else if (badge === "DIP") {
    cls = "badge-dip";
    label = `📉 Dip · ${score}/100`;
  } else if (badge === "FAIR") {
    cls = "badge-fair";
    label = `⚖️ Fair · ${score}/100`;
  } else if (badge === "PREMIUM") {
    cls = "badge-premium";
    label = `🚀 Growth · ${score}/100`;
  } else if (badge === "TRAP_RISK") {
    cls = "badge-trap";
    label = "⚠️ Value Trap";
  }

  return el("span.tag", {
    cls,
    text: label,
    title: `Value Score: ${score}/100 (${badge})`,
  });
}

function stockCandidateRow(c, { yours = false, onCompare = null } = {}) {
  const label = yours
    ? el("span", el("strong", { text: c.instrument }), " ", el("span.tag.ok", { text: "what you hold" }))
    : el(
        "span",
        el("button.linklike", {
          type: "button",
          text: c.instrument,
          title: `Add ${c.instrument} to comparison chart`,
          onclick: () => onCompare && onCompare(c.instrument),
        }),
        " ",
        el("span.muted", { text: c.name }),
      );

  return row([
    { node: label, cls: "name" },
    { text: c.sector || "—", cls: "prose muted" },
    {
      text: c.forward_pe != null ? `${Number(c.forward_pe).toFixed(1)}x` : "—",
      cls: c.forward_pe != null && c.forward_pe < 15 ? "gain num" : "num",
    },
    { text: c.price_to_book != null ? `${Number(c.price_to_book).toFixed(1)}x` : "—", cls: "num" },
    { text: c.ev_ebitda != null ? `${Number(c.ev_ebitda).toFixed(1)}x` : "—", cls: "num" },
    {
      text: c.fcf_yield != null ? f.pct(c.fcf_yield) : "—",
      cls: c.fcf_yield != null && c.fcf_yield > 0.05 ? "gain num" : "num",
    },
    {
      text: c.pct_off_52w_high != null ? f.pct(c.pct_off_52w_high) : "—",
      cls: c.pct_off_52w_high != null && c.pct_off_52w_high <= -0.2 ? "loss num" : "num",
    },
    {
      text: c.target_upside_pct != null ? f.pct(c.target_upside_pct) : "—",
      cls: c.target_upside_pct != null && c.target_upside_pct > 0 ? "gain num" : "num",
    },
    { node: valueScoreBadge(c.value_score, c.value_badge) },
  ]);
}

export function stockBargainFinderSection(onCompare) {
  const container = el("section", heading("Stock Bargain Finder — Undervalued & Pullback Opportunities"));
  container.append(
    el("p.sub", {
      text: "Scan and filter single stocks across the universe by valuation multiples (Forward P/E, FCF Yield, P/B) and discount from 52-week highs.",
    }),
  );

  const slot = el("div", el("p.muted", { text: "Loading stock fundamentals..." }));
  container.append(slot);

  get("/api/stocks/fundamentals").then((res) => {
    if (isError(res) || !res.stocks || !res.stocks.length) {
      slot.replaceChildren(el("p.note", { text: "No stock fundamentals recorded yet." }));
      return;
    }
    const allStocks = res.stocks;
    const sectors = res.sectors || [];

    let selectedSector = "";
    let selectedBadge = "";

    const sectorSelect = el(
      "select",
      el("option", { value: "", text: "All Sectors" }),
      sectors.map((s) => el("option", { value: s, text: s })),
    );

    const badgeSelect = el(
      "select",
      el("option", { value: "", text: "All Ratings" }),
      el("option", { value: "BARGAIN", text: "🏷️ Bargains Only (Score ≥ 70)" }),
      el("option", { value: "DIP", text: "📉 Pullback Dips Only (≥20% off 52W high)" }),
      el("option", { value: "BARGAIN_OR_DIP", text: "⭐ Bargains & Dips" }),
      el("option", { value: "FAIR", text: "⚖️ Fair Value" }),
      el("option", { value: "PREMIUM", text: "🚀 Growth / Premium" }),
      el("option", { value: "TRAP_RISK", text: "⚠️ Value Trap Warnings" }),
    );

    const peSlider = el("input.slider-range-input", { type: "range", min: "5", max: "60", step: "1", value: "60" });
    const peBadge = el("span.slider-filter-badge", { text: "Any P/E" });

    const discountSlider = el("input.slider-range-input", { type: "range", min: "0", max: "50", step: "5", value: "0" });
    const discountBadge = el("span.slider-filter-badge", { text: "Any" });

    const fcfSlider = el("input.slider-range-input", { type: "range", min: "0", max: "12", step: "1", value: "0" });
    const fcfBadge = el("span.slider-filter-badge", { text: "Any" });

    const tableWrap = el("div.wrap");

    const columns = [
      {
        label: "Ticker",
        key: "symbol",
        render: (s) => ({
          node: el("button.linklike", {
            type: "button",
            text: s.symbol,
            title: `Add ${s.symbol} to comparison chart`,
            onclick: () => onCompare && onCompare(s.symbol),
          }),
        }),
      },
      {
        label: "Name",
        key: "name",
        cls: "name",
        render: (s) => ({ text: s.name, cls: "name" }),
      },
      {
        label: "Sector",
        key: (s) => s.sector || "",
        cls: "prose muted",
        render: (s) => ({ text: s.sector || "—", cls: "prose muted" }),
      },
      {
        label: "Price",
        key: (s) => f.num(s.current_price),
        type: "num",
        render: (s) => ({
          text: f.money(s.current_price, s.currency || "EUR"),
          cls: "num",
        }),
      },
      {
        label: "Fwd P/E",
        tooltip: "Forward Price to Earnings ratio. Lower indicates cheaper relative to expected earnings.",
        key: (s) => (s.forward_pe != null && s.forward_pe > 0 ? Number(s.forward_pe) : 9999),
        type: "num",
        render: (s) => ({
          text: s.forward_pe != null ? `${Number(s.forward_pe).toFixed(1)}x` : "—",
          cls: s.forward_pe != null && s.forward_pe < 15 ? "gain num" : "num",
        }),
      },
      {
        label: "P/B",
        tooltip: "Price to Book ratio. Measures market price relative to book value.",
        key: (s) => (s.price_to_book != null && s.price_to_book > 0 ? Number(s.price_to_book) : 9999),
        type: "num",
        render: (s) => ({
          text: s.price_to_book != null ? `${Number(s.price_to_book).toFixed(1)}x` : "—",
          cls: "num",
        }),
      },
      {
        label: "FCF Yield",
        tooltip: "Free Cash Flow Yield = Free Cash Flow / Market Cap. Higher indicates strong cash generation.",
        key: (s) => f.num(s.fcf_yield),
        type: "pct",
        render: (s) => ({
          text: f.pct(s.fcf_yield),
          cls: s.fcf_yield != null && s.fcf_yield > 0.05 ? "gain num" : "num",
        }),
      },
      {
        label: "vs 52W High",
        tooltip: "Percentage discount from 52-week high price.",
        key: (s) => f.num(s.pct_off_52w_high),
        type: "pct",
        render: (s) => ({
          text: f.pct(s.pct_off_52w_high),
          cls: s.pct_off_52w_high != null && s.pct_off_52w_high <= -0.2 ? "loss num" : "num",
        }),
      },
      {
        label: "Target Upside",
        tooltip: "Wall Street consensus price target upside percentage.",
        key: (s) => f.num(s.target_upside_pct),
        type: "pct",
        render: (s) => ({
          text: f.pct(s.target_upside_pct, { sign: true }),
          cls: s.target_upside_pct != null && s.target_upside_pct > 0 ? "gain num" : "num",
        }),
      },
      {
        label: "Value Score",
        tooltip: "Composite value score (0-100) based on multiples, pullback discount, consensus upside, and solvency.",
        key: (s) => s.value_score ?? 0,
        type: "num",
        defaultSort: true,
        defaultDir: "desc",
        render: (s) => ({
          node: valueScoreBadge(s.value_score, s.value_badge),
        }),
      },
      {
        label: "Assessment",
        key: "value_narrative",
        sortable: false,
        render: (s) => ({
          text: s.value_narrative || "—",
          cls: "prose small muted",
          style: "max-width: 260px;",
        }),
      },
      {
        label: "",
        sortable: false,
        render: (s) => ({
          node: el("button.small", {
            type: "button",
            text: "+ Compare",
            title: `Add ${s.symbol} to comparison chart`,
            onclick: () => onCompare && onCompare(s.symbol),
          }),
        }),
      },
    ];

    function renderTable(stocks) {
      const tbl = sortableTable(columns, stocks, {
        placeholder: "Filter stocks by ticker, name, sector...",
        searchKeys: ["symbol", "name", "sector", "value_badge"],
        emptyText: "No stocks match the selected filters.",
        pageSize: 15,
        allowShowAll: true,
      });
      tableWrap.replaceChildren(tbl);
    }

    function update() {
      const maxPeVal = Number(peSlider.value);
      const minDiscVal = Number(discountSlider.value);
      const minFcfVal = Number(fcfSlider.value);

      peBadge.textContent = maxPeVal >= 60 ? "Any P/E" : `≤ ${maxPeVal}x`;
      discountBadge.textContent = minDiscVal === 0 ? "Any" : `≥ ${minDiscVal}% off`;
      fcfBadge.textContent = minFcfVal === 0 ? "Any" : `≥ ${minFcfVal}%`;

      const filtered = allStocks.filter((s) => {
        if (selectedSector && s.sector !== selectedSector) return false;
        if (selectedBadge === "BARGAIN" && s.value_badge !== "BARGAIN") return false;
        if (selectedBadge === "DIP" && s.value_badge !== "DIP") return false;
        if (selectedBadge === "BARGAIN_OR_DIP" && s.value_badge !== "BARGAIN" && s.value_badge !== "DIP") return false;
        if (selectedBadge === "FAIR" && s.value_badge !== "FAIR") return false;
        if (selectedBadge === "PREMIUM" && s.value_badge !== "PREMIUM") return false;
        if (selectedBadge === "TRAP_RISK" && s.value_badge !== "TRAP_RISK") return false;

        if (maxPeVal < 60) {
          if (s.forward_pe === null || s.forward_pe === undefined || s.forward_pe > maxPeVal || s.forward_pe <= 0) {
            return false;
          }
        }
        if (minDiscVal > 0) {
          if (s.pct_off_52w_high === null || s.pct_off_52w_high === undefined) return false;
          if (Math.abs(s.pct_off_52w_high * 100) < minDiscVal) return false;
        }
        if (minFcfVal > 0) {
          if (s.fcf_yield === null || s.fcf_yield === undefined) return false;
          if (s.fcf_yield * 100 < minFcfVal) return false;
        }
        return true;
      });

      renderTable(filtered);
    }

    sectorSelect.onchange = (e) => { selectedSector = e.target.value; update(); };
    badgeSelect.onchange = (e) => { selectedBadge = e.target.value; update(); };
    peSlider.oninput = () => update();
    discountSlider.oninput = () => update();
    fcfSlider.oninput = () => update();

    const controls = el(
      "div.slider-filter-panel",
      el(
        "div.slider-filter-group",
        el("div.slider-filter-header", el("span", { text: "Sector Filter" })),
        sectorSelect,
      ),
      el(
        "div.slider-filter-group",
        el("div.slider-filter-header", el("span", { text: "Valuation Rating" })),
        badgeSelect,
      ),
      el(
        "div.slider-filter-group",
        el("div.slider-filter-header", el("span", { text: "Max Forward P/E" }), peBadge),
        peSlider,
        el("div.slider-range-labels", el("span", { text: "5x" }), el("span", { text: "Any" })),
      ),
      el(
        "div.slider-filter-group",
        el("div.slider-filter-header", el("span", { text: "Min Dip from 52W High" }), discountBadge),
        discountSlider,
        el("div.slider-range-labels", el("span", { text: "Any" }), el("span", { text: "-50%" })),
      ),
      el(
        "div.slider-filter-group",
        el("div.slider-filter-header", el("span", { text: "Min Free Cash Flow Yield" }), fcfBadge),
        fcfSlider,
        el("div.slider-range-labels", el("span", { text: "Any" }), el("span", { text: "12%" })),
      ),
    );

    slot.replaceChildren(controls, tableWrap);
    update();
  });

  return container;
}

function scanPanel(scan, onCompare) {
  const s = scan.subject;
  const isStock = scan.is_stock || s.bucket === "EQ_SINGLE_STOCK" || scan.bucket === "EQ_SINGLE_STOCK";
  let activeMode = isStock ? "value" : "momentum";

  const tableWrap = el("div.wrap");

  function renderTable() {
    if (isStock && activeMode === "value") {
      const stockCols = [
        "Instrument", "Sector", "Fwd P/E", "P/B", "EV/EBITDA", "FCF Yield",
        "vs 52W High", "Target Upside", "Value Assessment"
      ];
      tableWrap.replaceChildren(
        el(
          "table",
          el("thead", el("tr", stockCols.map((c) => el("th", { text: c })))),
          el(
            "tbody",
            [
              stockCandidateRow(s, { yours: true }),
              ...scan.candidates.map((c) => stockCandidateRow(c, { onCompare })),
            ],
          ),
        ),
      );
    } else {
      const fundCols = [
        "Instrument", "Risk", "3 months", "6 months", "1 year", "vs yours",
        "Volatility", "Sharpe", "Fee"
      ];
      tableWrap.replaceChildren(
        el(
          "table",
          el("thead", el("tr", fundCols.map((c) => el("th", { text: c })))),
          el(
            "tbody",
            [
              candidateRow(s, { yours: true }),
              ...scan.candidates.map((c) => candidateRow(c, { onCompare })),
            ],
          ),
        ),
      );
    }
  }

  renderTable();

  let modeToggle = null;
  if (isStock) {
    const valBtn = el("button.stock-toggle-btn.active", {
      type: "button",
      text: "Valuation (Cheapest Peers)",
      onclick: () => {
        activeMode = "value";
        valBtn.classList.add("active");
        momBtn.classList.remove("active");
        renderTable();
      },
    });
    const momBtn = el("button.stock-toggle-btn", {
      type: "button",
      text: "Past Returns & Sharpe",
      onclick: () => {
        activeMode = "momentum";
        momBtn.classList.add("active");
        valBtn.classList.remove("active");
        renderTable();
      },
    });
    modeToggle = el("div.stock-toggle-group", valBtn, momBtn);
  }

  const subNote = isStock
    ? `Single stock peer comparison in ${s.sector || "sector"}. Ranked by fundamental valuation & cheapness (Fwd P/E, FCF Yield, 52W discount) rather than past momentum.`
    : `Risk category ${BUCKET_LABEL[scan.bucket] || scan.bucket}` +
      (scan.risk_level ? ` (Risk Level ${scan.risk_level}/7)` : "") +
      (scan.vol_band ? `, ${scan.vol_band.toLowerCase()} volatility` : "") +
      `. ${scan.considered} comparable instrument(s) considered. Returns are in euros.`;

  const headerRow = el(
    "div",
    { style: "display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; flex-wrap: wrap; gap: 0.5rem;" },
    el("h3", { text: `${s.instrument} — ${s.name}`, style: "margin: 0;" }),
    modeToggle,
  );

  const panel = el("div.panel", headerRow, el("p.note", { text: subNote }), tableWrap);

  if (s.value_narrative) {
    panel.append(el("p.note", { text: `Holding Valuation: ${s.value_narrative}` }));
  }

  const overlaps = scan.candidates.filter(
    (c) => c.overlap_pct !== null && c.overlap_pct !== undefined,
  );
  if (overlaps.length) {
    panel.append(
      el("p.note", {
        text:
          "Holds some of the same things as yours: " +
          overlaps.map((c) => `${c.instrument} ${f.pct(c.overlap_pct)}`).join(", ") +
          ".",
      }),
    );
  }

  if (scan.caveats?.length) {
    panel.append(el("ul.caveats", scan.caveats.map((x) => el("li", { text: x }))));
  }
  panel.append(
    explainPanel(
      `/api/peers/${encodeURIComponent(s.instrument)}`,
      {},
      `Write ${s.instrument} and its alternatives up in sentences`,
    ),
  );
  return panel;
}

/** One row of the comparison: yours, or an alternative to it. */
function candidateRow(c, { yours = false, onCompare = null } = {}) {
  const label = yours
    ? el(
        "span",
        el("strong", { text: c.instrument }),
        " ",
        el("span.tag.ok", { text: "what you hold" }),
      )
    : el(
        "span",
        el("button.linklike", {
          type: "button",
          text: c.instrument,
          title: `Add ${c.instrument} to the comparison chart above.`,
          onclick: () => onCompare && onCompare(c.instrument),
        }),
        " ",
        el("span.muted", { text: c.name }),
        c.comparable
          ? null
          : el("span.tag.warn", { text: "not like-for-like", title: (c.caveats || []).join(" ") }),
      );

  const delta = yours ? null : c.ret_1y_delta;
  const sharpeVal = c.sharpe_3y ?? c.sharpe_1y;
  return row([
    { node: label, cls: "name" },
    { node: riskBadge(c.risk_level) },
    { text: f.pct(c.ret_3m, { sign: true }), cls: f.tone(c.ret_3m) },
    { text: f.pct(c.ret_6m, { sign: true }), cls: f.tone(c.ret_6m) },
    { text: f.pct(c.ret_1y, { sign: true }), cls: f.tone(c.ret_1y) },
    yours ? { text: "—", cls: "muted" } : { text: f.pp(delta), cls: f.tone(delta) },
    { text: f.pct(c.vol_1y ?? c.vol_3y) },
    {
      text: sharpeVal !== null && sharpeVal !== undefined ? Number(sharpeVal).toFixed(2) : "—",
      cls: sharpeVal && Number(sharpeVal) > 0 ? "gain" : "",
      title: "Sharpe ratio (risk-adjusted return)",
    },
    { text: c.ter_bps === null || c.ter_bps === undefined ? "—" : f.bps(c.ter_bps) },
  ]);
}
