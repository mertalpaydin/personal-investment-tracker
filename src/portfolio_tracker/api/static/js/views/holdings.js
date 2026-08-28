/*
 * Every position, over one chosen window.
 *
 * Reads the same endpoint as the front page, so the two cannot disagree about
 * what a month is or what a holding returned over it.
 *
 * Two return columns, always both. "Its own return" is what the holding did on
 * its own money; "share of the total" is how many points of the portfolio's
 * return came from it. A holding up 74% that is 4% of the portfolio shows +74%
 * and about +2 points, and showing only the second reads as though it had
 * barely moved.
 *
 * The gain column is the true euro gain -- proceeds at today's rate less cost
 * at the rate on the day you bought. A tracker converting the whole gain at
 * today's rate reports a different, larger number, which is why the split is
 * on the tooltip rather than hidden.
 */

import { el, row, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";

const RANGES = [
  ["1D", "1 day"],
  ["1W", "1 week"],
  ["1M", "1 month"],
  ["3M", "3 months"],
  ["6M", "6 months"],
  ["YTD", "This year"],
  ["1Y", "1 year"],
  ["ALL", "All"],
];

let range = "1M";
let sortKey = "value_eur";
let sortDesc = true;

const COLUMNS = [
  { key: "instrument", label: "Ticker", text: true, hint: null },
  { key: "name", label: "Name", text: true, hint: null },
  { key: "quantity", label: "Units", hint: null },
  { key: "price_local", label: "Price", hint: "Latest close, in the currency it trades in." },
  { key: "value_eur", label: "Value", hint: null },
  {
    key: "value_change_eur",
    label: "Change",
    hint: "How much its value moved over the window chosen above, in euros.",
  },
  {
    key: "own_return",
    label: "Its own return",
    hint: "What this holding earned on its own money, per unit — so buying more of it does not read as a gain.",
  },
  {
    key: "contribution",
    label: "Share of the total",
    hint: "How many percentage points of the whole portfolio's return came from this holding. A big gain on a small position is a small share.",
  },
  {
    key: "unrealized_eur",
    label: "Gain so far",
    hint: "Gain on what you still hold, if you sold today. Frozen at the exchange rate you bought at, not today's.",
  },
];

export async function render({ query }) {
  if (query.get("range")) range = query.get("range").toUpperCase();

  const data = await all({
    over: get("/api/overview", { range }),
    positions: get("/api/positions"),
  });
  if (isError(data.over)) throw data.over;

  const o = data.over;
  // Price and quantity come from the positions endpoint; everything
  // window-dependent from the overview. Joined on the ticker.
  const detail = new Map(
    isError(data.positions)
      ? []
      : (data.positions.positions || []).map((p) => [p.instrument, p]),
  );
  const rows = (o.holdings || []).map((h) => ({ ...detail.get(h.instrument), ...h }));

  const view = el(
    "div",
    el("h1", { text: "Holdings" }),
    el("p.sub", {
      text: `${rows.length} open ${rows.length === 1 ? "position" : "positions"}, valued to ${f.date(o.window.to)}.`,
    }),
  );
  view.append(rangePicker(o));

  if (!rows.length) {
    view.append(
      el(
        "div.empty",
        el("p", { text: "There are no open positions." }),
        el("p", el("a", { href: "#/trades", text: "Record a trade" })),
      ),
    );
    return view;
  }

  view.append(table(rows, o));
  return view;
}

function rangePicker(o) {
  const box = el("div.ranges");
  for (const [key, label] of RANGES) {
    box.append(
      el("button", {
        text: label,
        "aria-pressed": String(key === range),
        onclick: () => {
          range = key;
          router.go("#/holdings?range=" + key);
        },
      }),
    );
  }
  box.append(el("span.over", { text: `${f.date(o.window.from)} to ${f.date(o.window.to)}` }));
  return box;
}

function withHint(label, hint) {
  return hint ? el("span.hint", { title: hint, text: label }) : el("span", { text: label });
}

function table(rows, o) {
  const sorted = [...rows].sort((a, b) => {
    const column = COLUMNS.find((c) => c.key === sortKey) || COLUMNS[4];
    const x = column.text ? a[column.key] : f.num(a[column.key]);
    const y = column.text ? b[column.key] : f.num(b[column.key]);
    // Nulls last regardless of direction: a missing figure is not the
    // smallest one, and sorting it to the top of a descending list claims it
    // is the largest.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    const cmp = column.text ? String(x).localeCompare(String(y)) : x - y;
    return sortDesc ? -cmp : cmp;
  });

  const head = el("tr");
  for (const column of COLUMNS) {
    head.append(
      el(
        "th.sortable",
        {
          role: "button",
          tabindex: "0",
          onclick: () => {
            if (sortKey === column.key) sortDesc = !sortDesc;
            else {
              sortKey = column.key;
              sortDesc = !column.text;
            }
            router.refresh();
          },
        },
        withHint(column.label, column.hint),
        sortKey === column.key ? el("span", { text: sortDesc ? " ↓" : " ↑" }) : null,
      ),
    );
  }

  const total = sorted.reduce((sum, r) => sum + (f.num(r.value_eur) ?? 0), 0);
  const body = sorted.map((r) => {
    const href = `#/holdings/${encodeURIComponent(r.instrument)}`;
    const weight = total ? (f.num(r.value_eur) ?? 0) / total : null;
    return row([
      {
        node: el(
          "span",
          el("a", { href, text: r.instrument }),
          r.valuation_status && r.valuation_status !== "OK"
            ? el("span.tag.warn", {
                text: "stale",
                title: `Priced from ${f.date(r.price_asof)}.`,
              })
            : null,
        ),
        cls: "name",
      },
      { text: r.name, cls: "name muted" },
      f.qty(r.quantity),
      { text: `${f.qty(r.price_local)} ${r.currency}`, cls: "muted" },
      {
        text: f.eur(r.value_eur),
        title: weight === null ? undefined : `${f.pct(weight, { digits: 1 })} of holdings`,
      },
      { text: f.eur(r.value_change_eur, { sign: true }), cls: f.tone(r.value_change_eur) },
      {
        text: f.pct(r.own_return, { sign: true }),
        cls: f.tone(r.own_return),
        title: r.held_from ? `measured from ${f.date(r.held_from)}` : undefined,
      },
      { text: f.pp(r.contribution), cls: f.tone(r.contribution) },
      {
        text: f.eur(r.unrealized_eur, { sign: true }),
        cls: f.tone(r.unrealized_eur),
        title:
          r.currency === "EUR" || r.unrealized_fx_eur === undefined
            ? undefined
            : `${f.eur(r.unrealized_local_eur, { sign: true })} from the price, ` +
              `${f.eur(r.unrealized_fx_eur, { sign: true })} from ${r.currency}`,
      },
    ]);
  });

  const foot = row([{ text: "Total", cls: "name" }, "", "", "", f.eur(total), "", "", "", ""]);

  return el(
    "section",
    el("div.wrap", el("table", el("thead", head), el("tbody", body), el("tfoot", foot))),
    el("p.note", {
      text: "Change and its own return follow the window above. Gain so far is life-to-date.",
    }),
  );
}

export { heading };
