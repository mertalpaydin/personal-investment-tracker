/*
 * Single stocks: what you watch, how cheap it is, how steady the business is.
 *
 * Split from the watchlist page for two reasons. ETFs and stocks are judged
 * on different things -- a fund on its fee and its risk group, a stock on its
 * valuation and its quality -- so one table could not serve both. And the
 * watchlist page carries the whole comparison universe with metrics; this
 * page asks only for what it shows, so it opens in well under a second.
 */

import { el, heading } from "../dom.js";
import { get, all, isError } from "../api.js";
import {
  addToComparison,
  comparisonChart,
  stockBargainFinderSection,
  watchedStockSection,
} from "./watchlist.js";

export async function render() {
  const data = await all({
    watchlist: get("/api/watchlist"),
    positions: get("/api/positions"),
    // Chart pool only: names and whether there are prices. No metrics.
    everything: get("/api/watchlist", { all: true, metrics: false }),
  });

  const entries = isError(data.watchlist) ? [] : data.watchlist.watchlist || [];
  const everything = isError(data.everything) ? [] : data.everything.watchlist || [];
  const holdings = isError(data.positions) ? [] : data.positions.positions || [];
  const stocks = entries.filter((e) => e.asset_class === "STOCK");

  const view = el(
    "div",
    el("h1", { text: "Stocks" }),
    el("p.sub", {
      text:
        "Watched single stocks with their Value and Quality scores, and a finder for " +
        "cheap or steady stocks across the whole universe. Add stocks to the watchlist " +
        "on the Watchlist page.",
    }),
  );

  if (holdings.length || everything.length) {
    view.append(comparisonChart(everything, holdings));
  }
  view.append(watchedStockSection(stocks, isError(data.watchlist) ? null : data.watchlist.basis));
  view.append(stockBargainFinderSection(addToComparison));
  return view;
}
