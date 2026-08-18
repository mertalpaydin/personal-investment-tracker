/*
 * uPlot, wrapped so every chart in this app looks like the same chart.
 *
 * Colours are read from the stylesheet's custom properties rather than
 * repeated here, so a line and the number beside it are the same green by
 * construction. The palette changes with the OS theme, and charts re-render
 * when it does -- otherwise a page left open through sunset keeps its light
 * axes on a dark background.
 *
 * Charts resize with their container via ResizeObserver. uPlot needs an
 * explicit pixel size, so something has to watch; doing it here means no view
 * has to remember.
 */

const registry = new Set();

/** Read a CSS custom property off :root. */
function token(name, fallback = "#888") {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export function palette() {
  return {
    fg: token("--fg", "#111"),
    muted: token("--muted", "#777"),
    line: token("--line", "#ddd"),
    gain: token("--gain", "#0a7d3c"),
    loss: token("--loss", "#b3261e"),
    accent: token("--accent", "#2563eb"),
    series: [
      token("--c1"), token("--c2"), token("--c3"),
      token("--c4"), token("--c5"), token("--c6"),
    ],
  };
}

/** Transparent version of a colour, for area fills under a line. */
function fade(colour, alpha = 0.13) {
  return `color-mix(in srgb, ${colour} ${Math.round(alpha * 100)}%, transparent)`;
}

const axisBase = (p) => ({
  stroke: p.muted,
  grid: { stroke: p.line, width: 1 },
  ticks: { stroke: p.line, width: 1 },
  font: '12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
});

const money = new Intl.NumberFormat("en-IE", { notation: "compact", maximumFractionDigits: 1 });
const plain = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 2 });

/** Value formatters by axis kind. */
const AXIS_VALUES = {
  eur: (_u, splits) => splits.map((v) => (v === null ? "" : `€${money.format(v)}`)),
  pct: (_u, splits) => splits.map((v) => (v === null ? "" : `${(v * 100).toFixed(0)}%`)),
  index: (_u, splits) => splits.map((v) => (v === null ? "" : plain.format(v))),
  plain: (_u, splits) => splits.map((v) => (v === null ? "" : plain.format(v))),
};

const LEGEND_VALUES = {
  eur: (_u, v) => (v === null ? "—" : `€${plain.format(v)}`),
  pct: (_u, v) => (v === null ? "—" : `${(v * 100).toFixed(2)}%`),
  index: (_u, v) => (v === null ? "—" : plain.format(v)),
  plain: (_u, v) => (v === null ? "—" : plain.format(v)),
};

/**
 * Draw a time-series chart into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {number[]} opts.x            - unix seconds, ascending
 * @param {Array<{label, data, kind?, colour?, fill?, dash?, width?, scale?}>} opts.series
 * @param {number} [opts.height=260]
 * @param {string} [opts.kind="eur"]   - default axis/legend formatting
 * @param {Array} [opts.axes]          - extra y-axis specs for secondary scales
 * @param {string} [opts.sync]         - sync key; charts sharing one share a cursor
 * @param {boolean} [opts.zero=false]  - force the y-axis to include zero
 * @param {Array} [opts.fillBetween]   - [[hiIndex, loIndex], …] shaded bands,
 *                                       indices into `series` (0-based)
 * @returns {object} the uPlot instance
 */
export function timeChart(container, opts) {
  const {
    x,
    series,
    height = 260,
    kind = "eur",
    sync = null,
    zero = false,
    legend = true,
  } = opts;

  const build = () => {
    const p = palette();
    const scales = { x: { time: true } };
    const usedScales = new Set(["y"]);

    const uSeries = [
      {
        value: (_u, ts) =>
          ts === null
            ? "—"
            : new Date(ts * 1000).toLocaleDateString("en-GB", {
                day: "numeric",
                month: "short",
                year: "numeric",
              }),
      },
    ];

    series.forEach((s, i) => {
      const colour = s.colour || p.series[i % p.series.length];
      const scale = s.scale || "y";
      usedScales.add(scale);
      uSeries.push({
        label: s.label,
        stroke: colour,
        width: s.width ?? 1.8,
        dash: s.dash || undefined,
        scale,
        fill: s.fill ? fade(colour, typeof s.fill === "number" ? s.fill : 0.13) : undefined,
        spanGaps: false,
        points: { show: false },
        value: LEGEND_VALUES[s.kind || kind] || LEGEND_VALUES.plain,
      });
    });

    for (const name of usedScales) {
      scales[name] = zero && name === "y" ? { range: (_u, min, max) => [Math.min(0, min), max] } : {};
    }

    const axes = [
      { ...axisBase(p), grid: { show: false } },
      { ...axisBase(p), scale: "y", values: AXIS_VALUES[kind] || AXIS_VALUES.plain, size: 62 },
    ];
    for (const extra of opts.axes || []) {
      axes.push({
        ...axisBase(p),
        side: 1,
        grid: { show: false },
        scale: extra.scale,
        values: AXIS_VALUES[extra.kind] || AXIS_VALUES.plain,
        size: 62,
      });
    }

    // A shaded band between two series -- the shape a range of outcomes
    // wants, as opposed to five lines the reader has to mentally fill in.
    const bands = (opts.fillBetween || []).map(([hi, lo], i) => ({
      series: [hi + 1, lo + 1],
      fill: fade(series[hi].colour || p.series[hi % p.series.length], i === 0 ? 0.1 : 0.16),
    }));

    return {
      width: Math.max(container.clientWidth || 640, 240),
      height,
      scales,
      series: uSeries,
      axes,
      bands,
      legend: { show: legend },
      cursor: sync ? { sync: { key: sync } } : {},
    };
  };

  container.replaceChildren();
  const data = [x, ...series.map((s) => s.data)];
  const chart = new uPlot(build(), data, container);

  const entry = { chart, container, build, data };
  registry.add(entry);

  const observer = new ResizeObserver(() => {
    const width = container.clientWidth;
    if (width > 0) chart.setSize({ width, height });
  });
  observer.observe(container);
  entry.observer = observer;

  return chart;
}

/**
 * Tear down every chart on the page.
 *
 * Views are replaced wholesale by the router, and a uPlot left behind keeps a
 * ResizeObserver and a window listener alive against a detached node. Called
 * from main.js before each render.
 */
export function destroyAll() {
  for (const entry of registry) {
    entry.observer?.disconnect();
    entry.chart.destroy();
  }
  registry.clear();
}

/**
 * Index a value series to 100 at its first defined point.
 *
 * This is what makes two holdings with different unit prices comparable on one
 * axis: the question is "how did each move", not "which costs more".
 * Returns nulls where the input was null, so gaps stay gaps.
 */
export function indexTo100(values, base = 100) {
  const first = values.find((v) => v !== null && v !== undefined && Number.isFinite(v) && v !== 0);
  if (first === undefined) return values.map(() => null);
  return values.map((v) =>
    v === null || v === undefined || !Number.isFinite(v) ? null : (v / first) * base,
  );
}

/** Unix seconds from an ISO date string, at local midnight. */
export function toTs(isoDate) {
  return new Date(`${String(isoDate).slice(0, 10)}T00:00:00`).getTime() / 1000;
}

// Re-render everything when the OS theme flips, so axis and grid colours
// follow. Rebuilding is simpler and more reliable than mutating live options.
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    for (const entry of registry) {
      const size = { width: entry.container.clientWidth, height: entry.chart.height };
      entry.chart.destroy();
      entry.chart = new uPlot(entry.build(), entry.data, entry.container);
      entry.chart.setSize(size);
    }
  });
}
