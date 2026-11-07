/*
 * Single stocks: what you watch, how cheap it is, how steady the business is.
 *
 * Split from the watchlist page for two reasons. ETFs and stocks are judged
 * on different things -- a fund on its fee and its risk group, a stock on its
 * valuation and its quality -- so one table could not serve both. And the
 * watchlist page carries the whole comparison universe with metrics; this
 * page asks only for what it shows, so it opens in well under a second.
 */

import { el, heading, row, table, replace } from "../dom.js";
import { get, put, all, isError } from "../api.js";
import { num, pct, tone } from "../fmt.js";
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
  view.append(scoreTrackRecordSection(everything));
  return view;
}

/*
 * Score track record: has a high score actually been followed by a better
 * return? The server keeps a dated snapshot of every score (with its parts)
 * and, once enough time has passed, compares it with the return since then.
 * Everything is measured against a benchmark instrument the user picks here.
 * Small watchlist, few months: this is a diagnostic, not proof.
 */
function scoreTrackRecordSection(pool) {
  const container = el("section.score-track-record.fit-table");
  const state = { score: "value" };

  const scoreSelect = el("select", { title: "Which score to check" });
  for (const [value, label] of [["value", "Value score"], ["quality", "Quality score"]]) {
    scoreSelect.append(el("option", { value, text: label }));
  }
  const benchSelect = el("select", { title: "Benchmark the returns are compared against" });
  benchSelect.append(el("option", { value: "", text: "Universe median (no benchmark)" }));
  const candidates = (pool || [])
    .filter((e) => e.asset_class !== "STOCK" && e.prices)
    .sort((a, b) => String(a.name || a.instrument).localeCompare(String(b.name || b.instrument)));
  for (const e of candidates) {
    const key = e.instrument;
    benchSelect.append(el("option", { value: key, text: `${e.name || key} (${key})` }));
  }

  const status = el("span.muted", { style: "font-size: .85rem; margin-left: .5rem;" });

  container.append(
    el(
      "div",
      { style: "display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:.5rem; margin-bottom:.5rem;" },
      el(
        "div",
        heading("Score track record"),
        el("p.sub", {
          text:
            "Did a high score lead to a better return? Every day the scores are saved with their " +
            "parts. Later, the return since that day is compared with the benchmark. " +
            "IC is the rank correlation between score and return on one day, averaged over days: " +
            "+1 is perfect foresight, 0 is noise, below 0 is backwards. Diagnostic, not proof.",
        }),
      ),
      el(
        "div",
        { style: "display:flex; align-items:center; gap:.4rem; padding-top:.25rem; flex-wrap:wrap;" },
        scoreSelect,
        benchSelect,
        status,
      ),
    ),
  );
  const slot = el("div", el("p.muted", { text: "Loading score track record..." }));
  container.append(slot);

  function fmtIc(v) {
    const x = num(v);
    return x === null ? { text: "—" } : { text: x.toFixed(2), cls: tone(x) };
  }
  function fmtPct(v, digits = 1) {
    const x = num(v);
    return x === null ? { text: "—" } : { text: pct(x, { digits, sign: true }), cls: tone(x) };
  }
  function fmtShare(v) {
    const x = num(v);
    return x === null ? { text: "—" } : { text: `${Math.round(x * 100)}%` };
  }

  function renderReport(report) {
    const cov = report.coverage || {};
    const out = el("div");
    if (!cov.snapshots) {
      out.append(
        el("p.nodata", {
          text:
            "No score snapshots yet. They are recorded by the daily update and by every " +
            "fundamentals refresh from now on.",
        }),
      );
      return out;
    }
    const benchText =
      report.benchmark_mode === "instrument" ? `benchmark ${report.benchmark}` : "universe median";
    out.append(
      el("p.muted", {
        style: "font-size:.85rem;",
        text:
          `${cov.snapshots} snapshots of ${cov.instruments} stocks over ${cov.days} day(s), ` +
          `${cov.first_date} to ${cov.last_date}; ${benchText}; one snapshot day per ${report.sample === "all" ? "day" : report.sample.replace("ly", "")}.`,
      }),
    );
    const mature = cov.mature_by_horizon || {};
    if (!Object.values(mature).some((n) => n > 0)) {
      const first = (cov.first_reading_by_horizon || {})["1m"];
      out.append(
        el("p.nodata", {
          text:
            `Collecting. Nothing is old enough to measure yet. ` +
            `The first 1-month reading is possible from ${first || "later"}.`,
        }),
      );
      return out;
    }

    for (const [version, stats] of Object.entries(report.versions || {})) {
      out.append(el("h3", { text: `Formula ${version}`, style: "margin:.75rem 0 .25rem;" }));
      const rows = stats.map((st) => {
        const top = st.buckets && st.buckets.length ? st.buckets[0] : null;
        const bottom = st.buckets && st.buckets.length ? st.buckets[st.buckets.length - 1] : null;
        return row([
          { text: st.horizon },
          { text: st.n_obs },
          { text: st.n_dates },
          fmtIc(st.mean_ic),
          { text: num(st.ic_tstat) === null ? "—" : num(st.ic_tstat).toFixed(1) },
          fmtShare(st.ic_positive_share),
          fmtShare(st.hit_rate),
          fmtPct(top && top.mean_rel),
          fmtPct(bottom && bottom.mean_rel),
          fmtPct(st.spread_top_minus_bottom),
        ]);
      });
      out.append(
        table(
          [
            "Horizon",
            "Stocks × days",
            "Days",
            "Mean IC",
            "t",
            "Days IC > 0",
            "Hit rate",
            "Top bucket",
            "Bottom bucket",
            "Spread",
          ],
          rows,
          { empty: "Nothing measurable yet for this version." },
        ),
      );

      const components = stats.length ? Object.keys(stats[0].component_ic || {}) : [];
      if (components.length) {
        const compRows = components.map((c) =>
          row([{ text: c.replace(/^[vq]_/, "").replace(/_/g, " ") }, ...stats.map((st) => fmtIc((st.component_ic || {})[c]))]),
        );
        out.append(
          el("p.muted", { style: "font-size:.85rem; margin:.5rem 0 .2rem;", text: "Which block predicts (mean IC of each block's points):" }),
        );
        out.append(table(["Block", ...stats.map((st) => st.horizon)], compRows));
      }
      const notes = [];
      for (const st of stats) for (const c of st.caveats || []) notes.push(`${st.horizon}: ${c}`);
      if (notes.length) {
        out.append(el("ul.muted", { style: "font-size:.8rem;" }, notes.map((n) => el("li", { text: n }))));
      }
    }
    if (report.caveats && report.caveats.length) {
      out.append(el("ul.muted", { style: "font-size:.8rem;" }, report.caveats.map((n) => el("li", { text: n }))));
    }
    return out;
  }

  async function load() {
    replace(slot, el("p.muted", { text: "Loading score track record..." }));
    let report;
    try {
      report = await get("/api/stocks/score-backtest", { score: state.score });
    } catch (err) {
      replace(slot, el("p.nodata", { text: `Could not load: ${err.message}` }));
      return;
    }
    benchSelect.value = report.benchmark_mode === "instrument" ? report.benchmark : "";
    replace(slot, renderReport(report));
  }

  scoreSelect.addEventListener("change", () => {
    state.score = scoreSelect.value;
    load();
  });
  benchSelect.addEventListener("change", async () => {
    status.textContent = "Saving...";
    try {
      await put("/api/stocks/score-backtest/benchmark", { import_key: benchSelect.value || null });
      status.textContent = "";
    } catch (err) {
      status.textContent = `Could not save: ${err.message}`;
    }
    load();
  });

  load();
  return container;
}
