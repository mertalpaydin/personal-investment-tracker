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

import { el, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";
import { sortableTable } from "../tables.js";

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

function table(rows, o) {
  const total = rows.reduce((sum, r) => sum + (f.num(r.value_eur) ?? 0), 0);

  const columns = [
    {
      label: "Ticker",
      key: "instrument",
      type: "str",
      render: (r) => {
        const href = `#/holdings/${encodeURIComponent(r.instrument)}`;
        return {
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
        };
      },
    },
    {
      label: "Name",
      key: "name",
      type: "str",
      render: (r) => ({ text: r.name, cls: "name muted" }),
    },
    {
      label: "Units",
      key: (r) => f.num(r.quantity),
      type: "num",
      render: (r) => ({ text: f.qty(r.quantity) }),
    },
    {
      label: "Price",
      key: (r) => f.num(r.price_local),
      type: "num",
      render: (r) => ({ text: `${f.qty(r.price_local)} ${r.currency || ""}`, cls: "muted" }),
    },
    {
      label: "Value",
      key: (r) => f.num(r.value_eur),
      type: "num",
      defaultSort: true,
      defaultDir: "desc",
      render: (r) => ({ text: f.eur(r.value_eur) }),
    },
    {
      label: "% of portfolio",
      key: (r) => {
        const portTotal = f.num(o.value?.now_eur) || total;
        return portTotal ? (f.num(r.value_eur) ?? 0) / portTotal : null;
      },
      type: "pct",
      render: (r) => {
        const portTotal = f.num(o.value?.now_eur) || total;
        const pctVal = portTotal ? (f.num(r.value_eur) ?? 0) / portTotal : null;
        return { text: f.pct(pctVal, { digits: 1 }) };
      },
    },
    {
      label: "Change",
      key: (r) => f.num(r.value_change_eur),
      type: "num",
      render: (r) => ({
        text: f.eur(r.value_change_eur, { sign: true }),
        cls: f.tone(r.value_change_eur),
      }),
    },
    {
      label: "Its own return",
      key: (r) => f.num(r.own_return),
      type: "pct",
      render: (r) => ({
        text: f.pct(r.own_return, { sign: true }),
        cls: f.tone(r.own_return),
        title: r.held_from ? `measured from ${f.date(r.held_from)}` : undefined,
      }),
    },
    {
      label: "Share of the total",
      key: (r) => f.num(r.contribution),
      type: "pct",
      render: (r) => ({
        text: f.pp(r.contribution),
        cls: f.tone(r.contribution),
      }),
    },
    {
      label: "Gain so far",
      key: (r) => f.num(r.unrealized_eur),
      type: "num",
      render: (r) => ({
        text: f.eur(r.unrealized_eur, { sign: true }),
        cls: f.tone(r.unrealized_eur),
        title:
          r.currency === "EUR" || r.unrealized_fx_eur === undefined
            ? undefined
            : `${f.eur(r.unrealized_local_eur, { sign: true })} from the price, ` +
              `${f.eur(r.unrealized_fx_eur, { sign: true })} from ${r.currency}`,
      }),
    },
  ];

  const tableNode = sortableTable(columns, rows, {
    placeholder: "Search holdings by ticker, name…",
    searchKeys: ["instrument", "name", "currency"],
  });

  return el(
    "section",
    tableNode,
    el("p.note", {
      text: "Change and its own return follow the window above. Gain so far is life-to-date.",
    }),
  );
}

export { heading };
