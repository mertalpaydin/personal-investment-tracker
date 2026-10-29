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
  const rows = (o.holdings || []).map((h) => {
    const p = detail.get(h.instrument) || {};
    return { ...p, ...h };
  });

  const view = el(
    "div.holdings-view",
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
          cls: "ticker",
        };
      },
    },
    {
      label: "Name",
      key: "name",
      type: "str",
      render: (r) => ({ text: r.name, cls: "name muted", title: r.name }),
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
      label: "% Port",
      tooltip: "Percentage of portfolio value",
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
      label: "Price Gain",
      tooltip: `Capital gain in selected ${range} window`,
      key: (r) => f.num(r.price_gain_eur),
      type: "num",
      render: (r) => ({
        text: f.eur(r.price_gain_eur, { sign: true }),
        cls: f.tone(r.price_gain_eur),
      }),
    },
    {
      label: "Price Ret",
      tooltip: `Price return in selected ${range} window`,
      key: (r) => f.num(r.price_return),
      type: "pct",
      render: (r) => ({
        text: f.has(r.price_return) ? f.pct(r.price_return, { sign: true }) : "—",
        cls: f.tone(r.price_return),
      }),
    },
    {
      label: "Dividends",
      tooltip: `Dividends received by currently held units in selected ${range} window`,
      key: (r) => f.num(r.dividends_eur),
      type: "num",
      render: (r) => ({
        text: f.num(r.dividends_eur) > 0 ? f.eur(r.dividends_eur) : "—",
        cls: f.num(r.dividends_eur) > 0 ? "gain" : "muted",
      }),
    },
    {
      label: "Div Ret",
      tooltip: `Dividend yield on currently held units in selected ${range} window`,
      key: (r) => f.num(r.dividend_return),
      type: "pct",
      render: (r) => ({
        text: f.num(r.dividend_return) > 0 ? f.pct(r.dividend_return) : "—",
        cls: f.num(r.dividend_return) > 0 ? "gain" : "muted",
      }),
    },
    {
      label: "Total Gain",
      tooltip: `Total gain (Price Gain + Dividends) in selected ${range} window`,
      key: (r) => f.num(r.total_gain_eur),
      type: "num",
      render: (r) => ({
        text: f.eur(r.total_gain_eur, { sign: true }),
        cls: f.tone(r.total_gain_eur),
      }),
    },
    {
      label: "Total Ret",
      tooltip: `Total return (Price Return + Dividend Return) in selected ${range} window`,
      key: (r) => f.num(r.total_return),
      type: "pct",
      render: (r) => ({
        text: f.has(r.total_return) ? f.pct(r.total_return, { sign: true }) : "—",
        cls: f.tone(r.total_return),
      }),
    },
  ];

  const tableNode = sortableTable(columns, rows, {
    placeholder: "Search holdings by ticker, name…",
    searchKeys: ["instrument", "name", "currency"],
  });

  const section = el(
    "section",
    tableNode,
    el("p.note", {
      text: `All gain and return figures in the table reflect your currently open holdings over the selected ${range} window (${f.date(o.window.from)} to ${f.date(o.window.to)}). Total Gain = Price Gain + Dividends. Total Return = Price Return + Dividend Return. Realized gains from closed trades and cumulative portfolio dividends received so far are shown below.`,
    }),
  );

  const divSection = cumulativeDividendsSection(o.cumulative_dividends);
  if (divSection) {
    section.append(divSection);
  }

  const closedSection = closedTradesSection(o.closed_trades);
  if (closedSection) {
    section.append(closedSection);
  }

  return section;
}

function cumulativeDividendsSection(data) {
  if (!data || !data.items || !data.items.length) return null;

  const panel = el("div.dividends-panel");
  const header = el(
    "div.panel-header",
    el(
      "div.panel-header-left",
      el("h3", { text: "Dividends Received So Far" }),
      el("p.sub", {
        text: "Cumulative dividends received across all holdings to date, including closed positions (sorted by latest payout date).",
      }),
    ),
    el(
      "div.panel-header-badge",
      el("span.badge-label", { text: "Total Dividends" }),
      el("strong.badge-value.gain", { text: f.eur(data.total_dividends_eur) }),
    ),
  );
  panel.append(header);

  const tableWrap = el("div.wrap");
  const tableEl = el("table.data-table");
  const thead = el(
    "thead",
    el(
      "tr",
      el("th", { text: "Holding" }),
      el("th", { text: "Name" }),
      el("th", { text: "Status" }),
      el("th.num", { text: "Payouts" }),
      el("th", { text: "First Received" }),
      el("th", { text: "Latest Payout" }),
      el("th.num", { text: "Total Received" }),
    ),
  );
  tableEl.append(thead);

  const tbody = el("tbody");
  for (const item of data.items) {
    const statusTag = item.is_open
      ? el("span.tag.ok", { text: "Open" })
      : el("span.tag.warn", { text: "Closed" });

    const tr = el(
      "tr",
      el(
        "td.ticker",
        el("a", {
          href: `#/holdings/${encodeURIComponent(item.instrument)}`,
          text: item.instrument,
        }),
      ),
      el("td.name.muted", { text: item.name || "—" }),
      el("td", statusTag),
      el("td.num", { text: `${item.payout_count} ${item.payout_count === 1 ? "payout" : "payouts"}` }),
      el("td.muted", { text: f.date(item.first_payout) }),
      el("td.muted", { text: f.date(item.last_payout) }),
      el("td.gain.num", { text: f.eur(item.total_amount_eur) }),
    );
    tbody.append(tr);
  }
  tableEl.append(tbody);
  tableWrap.append(tableEl);
  panel.append(tableWrap);

  return panel;
}

function closedTradesSection(data) {
  if (!data || !data.trades || !data.trades.length) return null;

  const panel = el("div.realized-trades-panel");
  const header = el(
    "div.panel-header",
    el(
      "div.panel-header-left",
      el("h3", { text: "Closed Trades (Realized Profit & Loss)" }),
      el("p.sub", {
        text: "Sales and disposals calculated via FIFO lot accounting (sorted by date). Shows actual profit/loss realized from selling shares.",
      }),
    ),
    el(
      "div.panel-header-badge",
      el("span.badge-label", { text: "Total Realized P&L" }),
      el("strong.badge-value", {
        class: f.tone(data.total_realized_gain_eur),
        text: f.eur(data.total_realized_gain_eur, { sign: true }),
      }),
    ),
  );
  panel.append(header);

  const tableWrap = el("div.wrap");
  const tableEl = el("table.data-table");
  const thead = el(
    "thead",
    el(
      "tr",
      el("th", { text: "Date" }),
      el("th", { text: "Holding" }),
      el("th.num", { text: "Units Sold" }),
      el("th.num", { text: "Proceeds" }),
      el("th.num", { text: "Cost Basis" }),
      el("th.num", { text: "Realized P&L" }),
      el("th.num", { text: "Return" }),
      el("th", { text: "Position Status" }),
    ),
  );
  tableEl.append(thead);

  const tbody = el("tbody");
  for (const t of data.trades) {
    const gainTone = f.tone(t.realized_gain_eur);
    const statusTag = t.is_closed_position
      ? el("span.tag.bad", { text: "Fully Closed" })
      : el("span.tag.ok", { text: "Remaining Held" });

    const holdingCell = el(
      "td.ticker",
      el("a", {
        href: `#/holdings/${encodeURIComponent(t.instrument)}`,
        text: t.instrument,
      }),
      t.name ? el("span.name.muted", { text: ` · ${t.name}` }) : null,
    );

    const tr = el(
      "tr",
      el("td.muted", { text: f.date(t.trade_date) }),
      holdingCell,
      el("td.num", {
        text: `${f.qty(t.qty_sold)} units${t.lot_count > 1 ? ` (${t.lot_count} lots)` : ""}`,
      }),
      el("td.num", { text: f.eur(t.proceeds_eur) }),
      el("td.muted.num", { text: f.eur(t.cost_basis_eur) }),
      el("td.num", { class: gainTone, text: f.eur(t.realized_gain_eur, { sign: true }) }),
      el("td.num", {
        class: gainTone,
        text: f.has(t.realized_return) ? f.pct(t.realized_return, { sign: true }) : "—",
      }),
      el("td", statusTag),
    );
    tbody.append(tr);
  }
  tableEl.append(tbody);
  tableWrap.append(tableEl);
  panel.append(tableWrap);

  return panel;
}

export { heading };
