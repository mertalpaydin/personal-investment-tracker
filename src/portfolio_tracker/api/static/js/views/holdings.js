/*
 * Every position, sortable, each row a way into its own page.
 *
 * The column that earns its place here is the gain split. For a foreign
 * holding the source export reports the local gain translated at today's
 * rate, which is not what was made; "reported +€2,108, actually +€1,088" is
 * the single most useful sentence this tool produces, and it belongs where
 * the holdings are listed rather than three clicks in.
 */

import { el, table, row } from "../dom.js";
import { get } from "../api.js";
import * as f from "../fmt.js";
import * as router from "../router.js";

const COLUMNS = [
  { key: "name", label: "Holding", sort: (r) => r.name || r.instrument, text: true },
  { key: "quantity", label: "Units", sort: (r) => f.num(r.quantity) },
  { key: "price_local", label: "Price", sort: (r) => f.num(r.price_local) },
  { key: "market_value_eur", label: "Value", sort: (r) => f.num(r.market_value_eur) },
  { key: "cost_basis_eur", label: "Cost", sort: (r) => f.num(r.cost_basis_eur) },
  { key: "unrealized_eur", label: "Gain", sort: (r) => f.num(r.unrealized_eur) },
  { key: "pct", label: "Gain %", sort: (r) => gainPct(r) },
];

function gainPct(r) {
  const cost = f.num(r.cost_basis_eur);
  const gain = f.num(r.unrealized_eur);
  if (cost === null || gain === null || cost === 0) return null;
  return gain / cost;
}

let sortKey = "market_value_eur";
let sortDesc = true;

export async function render() {
  const body = await get("/api/positions");
  const positions = body.positions || [];

  const view = el(
    "div",
    el("h1", { text: "Holdings" }),
    el("p.sub", {
      text: `${positions.length} open ${positions.length === 1 ? "position" : "positions"} · valued to ${f.date(body.as_of)}`,
    }),
  );

  if (!positions.length) {
    view.append(
      el(
        "div.empty",
        el("p", { text: "There are no open positions." }),
        el("p", el("a", { href: "#/trades", text: "Record a trade" })),
      ),
    );
    return view;
  }

  const total = positions.reduce((sum, r) => sum + (f.num(r.market_value_eur) ?? 0), 0);
  view.append(buildTable(positions, total));

  const anyForeign = positions.some((r) => r.currency !== "EUR");
  if (anyForeign) {
    view.append(
      el("p.note", {
        text:
          "Gain is the true euro gain: proceeds at today's rate minus cost at " +
          "the rate on the day you bought. A tracker that converts the whole " +
          "gain at today's rate reports a different, larger number.",
      }),
    );
  }
  return view;
}

function buildTable(positions, total) {
  const sorted = [...positions].sort((a, b) => {
    const column = COLUMNS.find((c) => c.key === sortKey) || COLUMNS[3];
    const x = column.sort(a);
    const y = column.sort(b);
    // Nulls last regardless of direction: a missing figure is not the
    // smallest figure, and sorting it to the top of a descending list would
    // say it was the largest.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    const cmp = column.text ? String(x).localeCompare(String(y)) : x - y;
    return sortDesc ? -cmp : cmp;
  });

  const head = el("tr");
  for (const column of COLUMNS) {
    const th = el("th.sortable", {
      text: column.label + (sortKey === column.key ? (sortDesc ? " ↓" : " ↑") : ""),
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
    });
    head.append(th);
  }

  const rows = sorted.map((r) => {
    const href = `#/holdings/${encodeURIComponent(r.instrument)}`;
    const weight = total ? (f.num(r.market_value_eur) ?? 0) / total : null;

    const nameCell = el(
      "span",
      el("a", { href, text: r.name || r.instrument }),
      r.valuation_status && r.valuation_status !== "OK"
        ? el("span.tag.warn", {
            text: r.valuation_status.toLowerCase(),
            title: `Priced from ${f.date(r.price_asof)}, ${r.price_stale_days} days before the valuation date.`,
          })
        : null,
      r.tax_regime && r.tax_regime !== "DE_INVESTMENT"
        ? el("span.tag", { text: "outside DE tax", title: r.tax_scope_note || "" })
        : null,
    );

    return row([
      { node: nameCell, cls: "name" },
      f.qty(r.quantity),
      { text: `${f.qty(r.price_local)} ${r.currency}`, cls: "muted" },
      {
        text: f.eur(r.market_value_eur),
        title: weight === null ? undefined : `${f.pct(weight, { digits: 1 })} of holdings`,
      },
      f.eur(r.cost_basis_eur),
      {
        text: f.eur(r.unrealized_eur, { sign: true }),
        cls: f.tone(r.unrealized_eur),
        title:
          r.currency === "EUR"
            ? undefined
            : `${f.eur(r.unrealized_local_eur, { sign: true })} from the price, ` +
              `${f.eur(r.unrealized_fx_eur, { sign: true })} from ${r.currency}`,
      },
      { text: f.pct(gainPct(r), { sign: true }), cls: f.tone(gainPct(r)) },
    ]);
  });

  const foot = row([
    { text: "Total", cls: "name" },
    "",
    "",
    f.eur(total),
    "",
    "",
    "",
  ]);

  return el(
    "div.wrap",
    el("table", el("thead", head), el("tbody", rows), el("tfoot", foot)),
  );
}

export { table };
