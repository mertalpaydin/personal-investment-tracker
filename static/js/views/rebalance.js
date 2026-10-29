/*
 * Target-weight portfolio rebalancing and smart cash inflow calculator.
 *
 * Helps maintain target asset allocations and models how monthly savings
 * or new deposits should be deployed to minimize drift without triggering
 * taxable sales.
 */

import { el, card, row, heading } from "../dom.js";
import { get, post, isError } from "../api.js";
import * as f from "../fmt.js";
import { sortableTable } from "../tables.js";

let currentPreset = "world_em_80_20";
let activeDimension = "risk_bucket";
let targets = {
  EQ_GLOBAL_BROAD: 0.8,
  EQ_EM: 0.2,
};
let cashInflow = 1000;

export async function render() {
  const container = el("div.view-rebalance");

  container.append(
    el("h1", { text: "Portfolio Rebalancing" }),
    el("p.sub", {
      text: "Compare your current portfolio weights against target allocations and model tax-efficient cash inflow deployments.",
    }),
  );

  const presetsData = await get("/api/rebalance/presets");
  const presets = presetsData.presets || [];

  const mainArea = el("div.rebalance-main");
  container.append(renderPresetChips(presets, mainArea), mainArea);

  await refreshCalculator(mainArea);
  return container;
}

function renderPresetChips(presets, mainArea) {
  const bar = el("div.chips", { style: "margin-bottom: 1.5rem; display: flex; flex-wrap: wrap; gap: 0.5rem;" });

  for (const p of presets) {
    const isSelected = p.id === currentPreset;
    const chip = el("button.chip", {
      type: "button",
      text: p.label,
      style: isSelected ? "font-weight: bold; border-color: var(--accent); background: color-mix(in srgb, var(--accent) 15%, var(--bg));" : "",
      onclick: async () => {
        currentPreset = p.id;
        activeDimension = p.dimension;
        targets = { ...p.targets };
        for (const sib of bar.children) {
          sib.style.fontWeight = "normal";
          sib.style.borderColor = "";
          sib.style.background = "";
        }
        chip.style.fontWeight = "bold";
        chip.style.borderColor = "var(--accent)";
        chip.style.backgroundColor = "color-mix(in srgb, var(--accent) 15%, var(--bg))";
        await refreshCalculator(mainArea);
      },
    });
    bar.append(chip);
  }

  return bar;
}

async function refreshCalculator(container) {
  container.replaceChildren(el("p.loading", { text: "Calculating allocations…" }));

  try {
    const res = await post("/api/rebalance/calculate", {
      dimension: activeDimension,
      targets,
      cash_inflow_eur: Number(cashInflow) || 0,
    });

    if (isError(res)) throw res;
    container.replaceChildren(renderResults(res, container));
  } catch (err) {
    container.replaceChildren(el("p.note.bad", { text: err.message || "Failed to calculate rebalance." }));
  }
}

function renderResults(plan, mainArea) {
  const wrap = el("div");

  // Inflow Simulator Input
  const inflowInput = el("input", {
    type: "number",
    min: "0",
    step: "100",
    value: String(cashInflow),
    style: "width: 8rem; font-weight: bold;",
  });

  const inflowForm = el(
    "div.panel",
    { style: "display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.5rem;" },
    el(
      "div",
      el("strong", { text: "Monthly / New Cash Inflow: " }),
      el("span.muted", { text: "Simulate smart buy orders to restore target weights without selling." }),
    ),
    el(
      "div",
      { style: "display: flex; align-items: center; gap: 0.5rem;" },
      el("span", { text: "€" }),
      inflowInput,
      el("button", {
        type: "button",
        text: "Recalculate",
        onclick: async () => {
          cashInflow = Number(inflowInput.value) || 0;
          await refreshCalculator(mainArea);
        },
      }),
    ),
  );

  // Summary Metrics
  const cards = el(
    "div.cols",
    { style: "margin-bottom: 1.5rem;" },
    card("Portfolio Value", f.eur(plan.total_current_value_eur)),
    card("Cash Inflow", f.eur(plan.cash_inflow_eur)),
    card("Post-Inflow Total", f.eur(plan.total_post_value_eur)),
    card("Max Allocation Drift", `${plan.max_drift_pp.toFixed(1)} pp`, {
      cls: plan.max_drift_pp > 5 ? "warn" : "",
    }),
  );

  // Target vs. Current Weights Table
  const columns = [
    { label: "Category / Bucket", key: "label", type: "str" },
    {
      label: "Current Value",
      key: (b) => b.current_value_eur,
      type: "num",
      render: (b) => ({ text: f.eur(b.current_value_eur) }),
    },
    {
      label: "Current %",
      key: (b) => b.current_weight,
      type: "pct",
      render: (b) => ({ text: f.pct(b.current_weight) }),
    },
    {
      label: "Target %",
      key: (b) => b.target_weight,
      type: "pct",
      render: (b) => ({
        text: f.pct(b.target_weight),
        cls: "accent",
      }),
    },
    {
      label: "Drift",
      key: (b) => b.drift_pp,
      type: "pct",
      render: (b) => {
        const sign = b.drift_pp > 0 ? "+" : "";
        const cls = Math.abs(b.drift_pp) > 5 ? (b.drift_pp > 0 ? "warn" : "loss") : "";
        return { text: `${sign}${b.drift_pp.toFixed(1)} pp`, cls };
      },
    },
    {
      label: "Inflow Deployment",
      key: (b) => b.inflow_allocation_eur,
      type: "num",
      render: (b) => ({
        text: b.inflow_allocation_eur > 0 ? `+${f.eur(b.inflow_allocation_eur)}` : "—",
        cls: b.inflow_allocation_eur > 0 ? "gain" : "muted",
      }),
    },
    {
      label: "Post-Inflow %",
      key: (b) => b.post_inflow_weight,
      type: "pct",
      render: (b) => ({ text: f.pct(b.post_inflow_weight) }),
    },
  ];

  const tableNode = sortableTable(columns, plan.buckets, {
    placeholder: "Filter buckets…",
    searchKeys: ["bucket", "label"],
  });

  // Suggested Execution Orders Section
  const orders = plan.suggested_orders || [];
  const ordersSection = el("section", heading("Recommended Deployment Orders"));

  if (!orders.length) {
    ordersSection.append(
      el("p.nodata", {
        text: "Portfolio is perfectly aligned with targets. No rebalancing trades required.",
      }),
    );
  } else {
    const orderColumns = [
      { label: "Action", key: "action", type: "str", render: (o) => ({ text: o.action, cls: o.action === "BUY" ? "gain" : "loss" }) },
      { label: "Ticker", key: "instrument", type: "str", render: (o) => ({ text: o.instrument, cls: "name" }) },
      { label: "Name", key: "name", type: "str", render: (o) => ({ text: o.name, cls: "name muted" }) },
      { label: "Amount", key: (o) => o.amount_eur, type: "num", render: (o) => ({ text: f.eur(o.amount_eur) }) },
      {
        label: "Est. Units",
        key: (o) => o.estimated_quantity,
        type: "num",
        render: (o) => ({
          text: o.estimated_quantity !== null && o.estimated_quantity !== undefined ? f.qty(o.estimated_quantity) : "—",
        }),
      },
      { label: "Reason", key: "reason", type: "str", render: (o) => ({ text: o.reason, cls: "muted" }) },
      {
        label: "",
        sortable: false,
        render: (o) => ({
          node: el("a.button.small", {
            href: `#/trades?instrument=${encodeURIComponent(o.instrument)}&type=${o.action}`,
            text: "Record Trade",
          }),
        }),
      },
    ];

    const orderTable = sortableTable(orderColumns, orders, {
      placeholder: "Filter orders…",
      searchKeys: ["instrument", "name", "action", "bucket"],
    });
    ordersSection.append(orderTable);
  }

  wrap.append(inflowForm, cards, heading("Allocation Breakdown"), tableNode, ordersSection);
  return wrap;
}
