/*
 * Labelled bars, in CSS.
 *
 * These replace the pie chart nobody needs. At the fifteen-to-twenty rows this
 * portfolio produces, a pie is a colour-matching exercise: the reader looks at
 * a wedge, finds it in the legend, and reads the number that was going to be
 * printed anyway. A sorted bar with its label and its figure on the same line
 * is faster to read, survives greyscale printing, and needs no library.
 *
 * Two shapes:
 *   bars()          - non-negative magnitudes, longest first (allocation)
 *   divergingBars() - signed values around a centre line (contributions)
 */

import { el } from "./dom.js";

/**
 * @param {Array<{label, value, right?, title?, href?}>} items - value is a fraction
 * @param {object} [opts]
 * @param {(v:number)=>string} [opts.format]
 * @param {string} [opts.colour] - CSS colour or custom property reference
 */
export function bars(items, { format = (v) => `${(v * 100).toFixed(1)}%`, colour = null } = {}) {
  const usable = items.filter((i) => Number.isFinite(i.value));
  if (!usable.length) return el("p.nodata", { text: "Nothing to show." });

  const max = Math.max(...usable.map((i) => Math.abs(i.value)), 0) || 1;
  const box = el("div.bars");

  for (const item of usable) {
    const width = `${((Math.abs(item.value) / max) * 100).toFixed(2)}%`;
    const label = item.href
      ? el("a", { href: item.href, text: item.label })
      : el("span", { text: item.label });

    box.append(
      el(
        "div.barrow",
        { title: item.title || undefined },
        el("span.lab", label),
        el("span.track", el("span.fill", { style: `width:${width}${colour ? `;background:${colour}` : ""}` })),
        el("span.val", { text: item.right ? `${format(item.value)} · ${item.right}` : format(item.value) }),
      ),
    );
  }
  return box;
}

/**
 * Bars either side of a centre line, for values that can be negative.
 *
 * The scale is symmetric around zero -- shared by the positive and negative
 * sides -- so a -3% bar and a +3% bar are the same length. Scaling each side
 * independently would make a small loss look as bad as a large gain.
 *
 * @param {Array<{label, value, title?, href?}>} items - value is a fraction
 */
export function divergingBars(items, { format = (v) => `${(v * 100).toFixed(2)}%` } = {}) {
  const usable = items.filter((i) => Number.isFinite(i.value));
  if (!usable.length) return el("p.nodata", { text: "Nothing to show." });

  const max = Math.max(...usable.map((i) => Math.abs(i.value)), 0) || 1;
  const box = el("div.bars");

  for (const item of usable) {
    const half = (Math.abs(item.value) / max) * 50;
    const negative = item.value < 0;
    const style =
      `width:${half.toFixed(2)}%;` +
      (negative
        ? `right:50%;background:var(--loss)`
        : `left:50%;background:var(--gain)`);

    const label = item.href
      ? el("a", { href: item.href, text: item.label })
      : el("span", { text: item.label });

    box.append(
      el(
        "div.barrow.diverge",
        { title: item.title || undefined },
        el("span.lab", label),
        el("span.track", el("span.fill", { style })),
        el("span.val", {
          class: item.value > 0 ? "gain" : item.value < 0 ? "loss" : "",
          text: format(item.value),
        }),
      ),
    );
  }
  return box;
}
