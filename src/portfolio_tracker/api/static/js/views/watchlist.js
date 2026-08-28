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
  view.append(watchTable(entries, isError(data.watchlist) ? null : data.watchlist.basis));

  if (entries.length) {
    const holdings = isError(data.positions) ? [] : data.positions.positions || [];
    view.append(comparisonChart(entries, holdings));
  }

  const buckets = (!isError(data.universe) && data.universe.buckets) || Object.keys(BUCKET_LABEL);
  if (!isError(data.peers)) view.append(peerSection(data.peers, buckets));
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
  const symbol = el("input", { type: "text", placeholder: "IWDA.DE", autocapitalize: "characters" });
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
      result.replaceChildren(found.found ? confirmForm(found) : el("p.note.bad", { text: found.problem }));
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

function comparisonChart(entries, holdings) {
  const box = el("div.chart");
  const pal = palette();
  const keys = [
    ...entries.filter((e) => e.prices).map((e) => e.instrument),
    ...holdings.slice(0, 3).map((h) => h.instrument),
  ].slice(0, 6);

  if (!keys.length) return el("div");

  queueMicrotask(async () => {
    const loaded = await all(
      Object.fromEntries(
        keys.map((k) => [k, get(`/api/instruments/${encodeURIComponent(k)}/series`)]),
      ),
    );
    // A shared date axis: the union of every series, so lines with different
    // start dates still line up rather than being drawn against each other's
    // indices.
    const dates = new Set();
    for (const k of keys) if (!isError(loaded[k])) for (const p of loaded[k].points) dates.add(p.date);
    const axis = [...dates].sort();
    if (axis.length < 2) return;

    const series = [];
    keys.forEach((k, i) => {
      if (isError(loaded[k])) return;
      const byDate = new Map(loaded[k].points.map((p) => [p.date, p.level]));
      const aligned = axis.map((d) => byDate.get(d) ?? null);
      series.push({
        label: k,
        data: indexTo100(aligned),
        colour: pal.series[i % pal.series.length],
        width: holdings.some((h) => h.instrument === k) ? 2.4 : 1.6,
        dash: holdings.some((h) => h.instrument === k) ? undefined : [4, 3],
      });
    });
    if (!series.length) return;
    timeChart(box, { x: axis.map(toTs), height: 300, kind: "index", series });
  });

  return el(
    "section",
    heading("Watched against held"),
    box,
    el("p.chart-note", {
      text:
        "All indexed to 100 at the start, in euros. Solid lines are holdings, " +
        "dashed are watched. Lines starting later simply had no price before then.",
    }),
  );
}

/* ----------------------------------------------------------------- peers */

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
 * refusal straight back as a click-through.
 */
function categoryRow(key, buckets) {
  const select = el(
    "select",
    el("option", { value: "", text: "Choose…" }),
    buckets.map((b) => el("option", { value: b, text: BUCKET_LABEL[b] || b })),
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
    { node: select },
    { node: save },
  ]);
}

function peerSection(body, buckets) {
  const scans = body.scans || [];
  const needs = body.needs_category || [];
  const section = el("section", heading("Comparable to what you hold"));

  // One paragraph across every holding, not one per holding. The question is
  // "is there something better than what I have", and asking it eleven times
  // separately is eleven calls against a free tier to say what one says.
  if (scans.length) {
    section.append(
      explainPanel("/api/peers", {}, "Compare my whole portfolio in words"),
    );
  }

  for (const scan of scans) section.append(scanPanel(scan));
  if (scans.length) section.append(el("p.note", { text: scans[0].disclaimer }));
  else section.append(el("p.note", { text: body.note }));

  // Listed, not compared. A guessed category would pair a semiconductor ETF
  // with a world tracker and report the difference to two decimal places.
  if (needs.length) {
    section.append(
      el(
        "div.panel.warn",
        el("h3", { text: "Not compared yet" }),
        el("p", {
          text:
            `${needs.join(", ")} have no risk category recorded, so there is nothing ` +
            "to say they are like. Comparing them against a broad tracker would " +
            "produce a precise number about two unlike things.",
        }),
        el("p.note", {
          text:
            "Pick what each one actually is. Nothing else on this page changes; " +
            "it only decides which instruments are treated as its peers.",
        }),
        el("div.wrap", el("table", el("tbody", needs.map((key) => categoryRow(key, buckets))))),
      ),
    );
  }
  return section;
}

function scanPanel(scan) {
  const s = scan.subject;
  const panel = el(
    "div.panel",
    el(
      "h3",
      { text: `${s.instrument} — ${s.name}` },
    ),
    el("p.note", {
      text:
        `Risk category ${scan.bucket}` +
        (scan.vol_band ? `, ${scan.vol_band.toLowerCase()} volatility` : "") +
        `. ${scan.considered} comparable instrument(s) considered.`,
    }),
  );

  panel.append(
    el(
      "div.wrap",
      el(
        "table",
        el(
          "thead",
          el(
            "tr",
            ["Alternative", "3 years a year", "vs yours", "Volatility", "Fee", "Overlap", ""].map(
              (t) => el("th", { text: t }),
            ),
          ),
        ),
        el(
          "tbody",
          scan.candidates.map((c) => sentenceRow(c, s)),
        ),
      ),
    ),
  );

  if (scan.caveats?.length) {
    panel.append(el("ul.caveats", scan.caveats.map((x) => el("li", { text: x }))));
  }
  panel.append(
    explainPanel(`/api/peers/${encodeURIComponent(s.instrument)}`, {}, "Put this in words"),
  );
  return panel;
}

function sentenceRow(c, subject) {
  const delta = c.ret_3y_ann_delta ?? c.ret_1y_delta;
  return row([
    {
      node: el(
        "span",
        el("strong", { text: c.instrument }),
        " ",
        el("span.muted", { text: c.name }),
        c.comparable
          ? null
          : el("span.tag.warn", {
              text: "not like-for-like",
              title: c.caveats.join(" "),
            }),
      ),
      cls: "name",
    },
    { text: f.pct(c.ret_3y_ann, { sign: true }), cls: f.tone(c.ret_3y_ann) },
    { text: f.pp(delta), cls: f.tone(delta) },
    {
      text: f.pct(c.vol_3y ?? c.vol_1y),
      title:
        c.vol_3y_delta === null || c.vol_3y_delta === undefined
          ? undefined
          : `${f.pp(c.vol_3y_delta)} vs yours`,
    },
    {
      text: c.ter_bps === null || c.ter_bps === undefined ? "—" : f.bps(c.ter_bps),
      cls: c.ter_bps_delta !== null && c.ter_bps_delta !== undefined && c.ter_bps_delta < 0 ? "gain" : "",
    },
    // Null overlap is unknown, not zero: it needs a published holdings file
    // for both funds, and saying 0% would claim they share nothing.
    { text: c.overlap_pct === null || c.overlap_pct === undefined ? "unknown" : f.pct(c.overlap_pct) },
    { node: el("a", { href: `#/holdings/${encodeURIComponent(subject.instrument)}`, text: "yours" }) },
  ]);
}
