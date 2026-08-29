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

export async function render() {
  const data = await all({
    watchlist: get("/api/watchlist"),
    positions: get("/api/positions"),
    peers: get("/api/peers"),
    universe: get("/api/universe"),
    suggested: get("/api/instruments/categories/suggested"),
    // Everything the tool can price, for the chart picker and the catalogue
    // listing. The watch table below still shows only what you chose.
    everything: get("/api/watchlist", { all: true }),
  });

  const view = el(
    "div",
    el("h1", { text: "Watchlist" }),
    el("p.sub", {
      text: "Instruments you do not own, measured the same way as the ones you do.",
    }),
  );

  view.append(addSection());
  if (!isError(data.universe)) view.append(universeSection(data.universe));

  const entries = isError(data.watchlist) ? [] : data.watchlist.watchlist || [];
  const everything = isError(data.everything) ? [] : data.everything.watchlist || [];
  const holdings = isError(data.positions) ? [] : data.positions.positions || [];

  // Always, not only when something is being watched. The chart is the
  // reason to come to this page, and it was hidden behind having first added
  // something by hand.
  if (holdings.length || everything.length) {
    view.append(comparisonChart(everything, holdings));
  }
  view.append(watchTable(entries, isError(data.watchlist) ? null : data.watchlist.basis));
  if (everything.length) view.append(catalogueSection(everything));

  const buckets = (!isError(data.universe) && data.universe.buckets) || Object.keys(BUCKET_LABEL);
  if (!isError(data.peers)) view.append(peerSection(data.peers, buckets, data.suggested, addToComparison));
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
  const key = el("input", { type: "text", value: found.suggested_key });
  const name = el("input", { type: "text", value: found.name || "" });
  const currency = el("input", { type: "text", value: found.currency || "EUR", maxlength: "3" });
  const ter = el("input", { type: "number", step: "1", min: "0", placeholder: "e.g. 20" });
  const bucket = el("input", { type: "text", placeholder: "e.g. EQ_GLOBAL_BROAD" });
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
      field("Short key", key, "what an import would match on — no .DE suffix"),
      field("Name", name),
      field("Currency", currency),
      field("Ongoing charge", ter, "basis points, from the fund's key information document"),
      field("Risk category", bucket, "optional; sharpens the comparison"),
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

  const rows = entries.map((e) => {
    const p = e.performance || {};
    return row([
      { node: el("strong", { text: e.instrument }), cls: "name" },
      { text: e.name, cls: "name muted" },
      { text: f.pct(p.ret_1y, { sign: true }), cls: f.tone(p.ret_1y) },
      { text: f.pct(p.ret_3y_ann, { sign: true }), cls: f.tone(p.ret_3y_ann) },
      f.pct(p.vol_1y),
      { text: f.pct(p.max_dd_3y), cls: p.max_dd_3y ? "loss" : "" },
      { node: terCell(e) },
      {
        node: el(
          "span",
          e.prices === 0 ? el("span.tag.warn", { text: "no prices yet" }) : null,
          p.basis === "PRICE" ? el("span.tag.warn", { text: "price only", title: (p.caveats || [])[0] }) : null,
          buyButton(e),
          removeButton(e),
        ),
      },
    ]);
  });

  return el(
    "section",
    heading("Being watched"),
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Ticker", "Name", "1 year", "3 years a year", "Volatility", "Worst fall", "Fee", ""].map(
              (t) => el("th", { text: t }),
            ),
          ),
        ),
        el("tbody", rows),
      ),
    ),
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

  const byBucket = new Map();
  for (const e of pool) {
    const key = e.risk_bucket || "Uncategorised";
    if (!byBucket.has(key)) byBucket.set(key, []);
    byBucket.get(key).push(e);
  }

  const groups = [...byBucket.entries()].sort((a, b) => b[1].length - a[1].length);
  return el(
    "section",
    heading("What is in the comparison set"),
    el(
      "details.assumptions",
      el("summary", { text: `${pool.length} instruments, by category` }),
      ...groups.map(([b, list]) =>
        el(
          "div.wrap",
          el("h3", { text: `${BUCKET_LABEL[b] || b} — ${list.length}` }),
          el(
            "table",
            el(
              "thead",
              el("tr", ["Ticker", "Name", "Fee", "Prices", ""].map((h) => el("th", { text: h }))),
            ),
            el(
              "tbody",
              list.map((e) =>
                row([
                  { text: e.instrument, cls: "name" },
                  { text: e.name, cls: "prose" },
                  { text: e.ter_pct === null || e.ter_pct === undefined ? "—" : f.pct(e.ter_pct) },
                  { text: e.prices ? `${e.prices}` : "none yet" },
                  {
                    node: el("button.small", {
                      type: "button",
                      text: "Chart it",
                      title: `Add ${e.instrument} to the comparison chart.`,
                      onclick: () => addToComparison(e.instrument),
                    }),
                  },
                ]),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

function comparisonChart(entries, holdings) {
  const pool = [
    ...holdings.map((h) => ({ key: h.instrument, name: h.name, held: true })),
    ...entries.filter((e) => e.prices).map((e) => ({ key: e.instrument, name: e.name })),
  ];
  if (!pool.length) return el("div");

  if (!compared.size) for (const h of holdings.slice(0, 4)) compared.add(h.instrument);

  const box = el("div.chart", { id: "compare-chart" });
  const chosen = el("div.chips");

  const draw = async () => {
    const keys = [...compared];
    chosen.replaceChildren(
      ...keys.map((k) =>
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
      ),
      keys.length ? null : el("span.muted", { text: "Nothing selected." }),
    );
    if (!keys.length) {
      box.replaceChildren();
      return;
    }
    const pal = palette();
    const loaded = await all(
      Object.fromEntries(
        keys.map((k) => [k, get(`/api/instruments/${encodeURIComponent(k)}/series`)]),
      ),
    );
    // A shared date axis: the union of every series, so lines with different
    // start dates line up rather than being drawn against each other's
    // indices.
    const dates = new Set();
    for (const k of keys) {
      if (!isError(loaded[k])) for (const pt of loaded[k].points) dates.add(pt.date);
    }
    const axis = [...dates].sort();
    if (axis.length < 2) return;

    const series = [];
    keys.forEach((k, i) => {
      if (isError(loaded[k])) return;
      const byDate = new Map(loaded[k].points.map((pt) => [pt.date, pt.level]));
      series.push({
        label: k,
        data: indexTo100(axis.map((d) => byDate.get(d) ?? null)),
        colour: pal.series[i % pal.series.length],
        width: holdings.some((h) => h.instrument === k) ? 2.4 : 1.6,
        dash: holdings.some((h) => h.instrument === k) ? undefined : [4, 3],
      });
    });
    if (series.length) timeChart(box, { x: axis.map(toTs), height: 320, kind: "index", series });
  };
  redrawComparison = draw;

  const option = (x) => el("option", { value: x.key, text: `${x.key} — ${x.name}` });
  const held = pool.filter((x) => x.held);
  const rest = pool.filter((x) => !x.held);
  // The attributes object has to be the *second* argument; passing it last
  // made the handler a child node and printed it as [object Object].
  const picker = el(
    "select",
    {
      onchange: (e) => {
        if (e.target.value) addToComparison(e.target.value);
        e.target.value = "";
      },
    },
    el("option", { value: "", text: "Add an instrument…" }),
    held.length ? el("optgroup", { label: "You hold" }, held.map(option)) : null,
    rest.length
      ? el("optgroup", { label: "Watched and comparison set" }, rest.map(option))
      : null,
  );

  queueMicrotask(draw);

  return el(
    "section",
    heading("Compare anything against anything"),
    el("div.actions", picker),
    chosen,
    box,
    el("p.chart-note", {
      text:
        "All indexed to 100 at the start, in euros, so lines of very different " +
        "prices can be read together. Solid is held, dashed is not. A line " +
        "starting later simply had no price before then.",
    }),
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
function categoryRow(key, buckets, proposal) {
  const select = el(
    "select",
    el("option", { value: "", text: "Choose…" }),
    buckets.map((b) =>
      el("option", {
        value: b,
        text: BUCKET_LABEL[b] || b,
        selected: proposal ? b === (proposal.current || proposal.suggested) : false,
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
    { node: select },
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
        for (const x of usable) categories[x.instrument] = x.suggested;
        await post("/api/instruments/categories", { categories });
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

  // One paragraph across every holding, not one per holding. The question is
  // "is there something better than what I have", and asking it eleven times
  // separately is eleven calls against a free tier to say what one says.
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

  // Listed, not compared. A guessed category would pair a semiconductor ETF
  // with a world tracker and report the difference to two decimal places.
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
                ["Holding", "Name", "Category", "Why", ""].map((h) => el("th", { text: h })),
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

function scanPanel(scan, onCompare) {
  const s = scan.subject;

  /*
   * Your own holding is the first row of the table.
   *
   * It used to show a candidate's volatility and fee with nothing to measure
   * them against, and 23% or 15bp mean nothing until they sit beside yours.
   *
   * Two columns are gone. "3 years a year" was empty in every row, because
   * there is one year of history to work from; "Overlap" needs a published
   * holdings file for both funds and so read "unknown" all the way down. A
   * column that is always empty is worse than an absent one -- it suggests a
   * figure exists and is being withheld.
   */
  const columns = ["Instrument", "3 months", "6 months", "1 year", "vs yours", "Volatility", "Fee"];

  const panel = el(
    "div.panel",
    el("h3", { text: `${s.instrument} — ${s.name}` }),
    el("p.note", {
      text:
        `Risk category ${scan.bucket}` +
        (scan.vol_band ? `, ${scan.vol_band.toLowerCase()} volatility` : "") +
        `. ${scan.considered} comparable instrument(s) considered. Returns are ` +
        "in euros over the period named, not annualised. Volatility is over one year.",
    }),
    el(
      "div.wrap",
      el(
        "table",
        el("thead", el("tr", columns.map((c) => el("th", { text: c })))),
        el(
          "tbody",
          [
            candidateRow(s, { yours: true }),
            ...scan.candidates.map((c) => candidateRow(c, { onCompare })),
          ],
        ),
      ),
    ),
  );

  // Overlap only where there is one, as a sentence rather than a column of
  // "unknown".
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
        // Clicking a ticker puts it on the chart above, which is what you
        // actually want to do with it. A link to a holdings page would 404:
        // nothing in the comparison set is held.
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
  return row([
    { node: label, cls: "name" },
    { text: f.pct(c.ret_3m, { sign: true }), cls: f.tone(c.ret_3m) },
    { text: f.pct(c.ret_6m, { sign: true }), cls: f.tone(c.ret_6m) },
    { text: f.pct(c.ret_1y, { sign: true }), cls: f.tone(c.ret_1y) },
    yours ? { text: "—", cls: "muted" } : { text: f.pp(delta), cls: f.tone(delta) },
    { text: f.pct(c.vol_1y ?? c.vol_3y) },
    { text: c.ter_bps === null || c.ter_bps === undefined ? "—" : f.bps(c.ter_bps) },
  ]);
}
