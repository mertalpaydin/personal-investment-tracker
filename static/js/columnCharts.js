/*
 * Interactive column charts for periodic aggregated returns (monthly and weekly).
 *
 * Designed to show the return (%) and value gain (EUR) in each specific period,
 * color-coded (green for gains, red for losses) around a clean zero baseline.
 * Hovering over any column displays an interactive tooltip with both % and € figures.
 */

import { el } from "./dom.js";
import * as f from "./fmt.js";

/**
 * Render an interactive column chart into container.
 *
 * @param {HTMLElement} container - DOM element to render into
 * @param {Array<{key, label, short_label, start_date, end_date, twr, value_eur}>} items
 * @param {object} [opts]
 * @param {number} [opts.height=250] - Chart height in pixels
 * @param {string} [opts.emptyText="No data for this window"]
 */
export function columnChart(container, items, { height = 250, emptyText = "No data for this window" } = {}) {
  container.replaceChildren();
  container.classList.add("agg-chart-wrapper");

  if (!items || !items.length) {
    container.append(el("p.nodata", { text: emptyText }));
    return;
  }

  const tooltip = el("div.agg-chart-tooltip");
  tooltip.style.display = "none";
  container.append(tooltip);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "agg-chart-svg");
  svg.setAttribute("height", String(height));
  svg.style.width = "100%";
  svg.style.height = `${height}px`;
  svg.style.display = "block";
  svg.style.overflow = "visible";
  container.append(svg);

  function draw() {
    const width = container.clientWidth || 400;
    svg.setAttribute("width", String(width));
    svg.replaceChildren();

    const padLeft = 48;
    const padRight = 14;
    const padTop = 20;
    const padBottom = 34;

    const plotWidth = Math.max(10, width - padLeft - padRight);
    const plotHeight = Math.max(10, height - padTop - padBottom);

    const twrs = items.map((d) => d.twr ?? 0);
    const rawMax = Math.max(0.01, ...twrs);
    const rawMin = Math.min(-0.01, ...twrs);

    // Pick clean, human-friendly round step (e.g. 1%, 2%, 2.5%, 5%, 10%)
    const span = Math.max(rawMax, Math.abs(rawMin));
    let step = 0.02; // default 2%
    if (span <= 0.035) step = 0.01;
    else if (span <= 0.07) step = 0.02;
    else if (span <= 0.14) step = 0.025;
    else if (span <= 0.28) step = 0.05;
    else step = 0.10;

    // Generate positive ticks above 0
    const posTicks = [];
    for (let t = step; t <= rawMax * 1.05; t += step) {
      posTicks.push(t);
    }
    if (!posTicks.length) posTicks.push(step);

    // Generate negative ticks below 0
    const negTicks = [];
    for (let t = -step; t >= rawMin * 1.05; t -= step) {
      negTicks.push(t);
    }
    if (!negTicks.length) negTicks.push(-step);

    const yMax = Math.max(rawMax * 1.15, posTicks[posTicks.length - 1] + step * 0.3);
    const yMin = Math.min(rawMin * 1.15, negTicks[negTicks.length - 1] - step * 0.3);

    // EXACT 0.0% baseline coordinate
    const yZero = padTop + ((yMax - 0) / (yMax - yMin)) * plotHeight;

    // Y Axis grid and ticks
    const gridGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    gridGroup.setAttribute("class", "agg-chart-grid");

    // Draw dashed lines for positive and negative ticks
    const gridTicks = [...posTicks, ...negTicks];
    for (const t of gridTicks) {
      const y = padTop + ((yMax - t) / (yMax - yMin)) * plotHeight;

      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(padLeft));
      line.setAttribute("x2", String(padLeft + plotWidth));
      line.setAttribute("y1", String(y));
      line.setAttribute("y2", String(y));
      line.setAttribute("stroke", "var(--line, #e2e8f0)");
      line.setAttribute("stroke-width", "1");
      line.setAttribute("stroke-dasharray", "3,3");
      gridGroup.append(line);

      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", String(padLeft - 8));
      text.setAttribute("y", String(y + 3.5));
      text.setAttribute("text-anchor", "end");
      text.setAttribute("fill", "var(--muted, #666)");
      text.setAttribute("font-size", "11px");
      text.setAttribute("font-family", 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
      text.textContent = `${t > 0 ? "+" : ""}${(t * 100).toFixed(step < 0.02 ? 1 : 0)}%`;
      gridGroup.append(text);
    }

    // ALWAYS draw the solid 0.0% baseline at exact yZero
    const zeroLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
    zeroLine.setAttribute("x1", String(padLeft));
    zeroLine.setAttribute("x2", String(padLeft + plotWidth));
    zeroLine.setAttribute("y1", String(yZero));
    zeroLine.setAttribute("y2", String(yZero));
    zeroLine.setAttribute("stroke", "var(--line-strong, #888)");
    zeroLine.setAttribute("stroke-width", "1.5");
    gridGroup.append(zeroLine);

    const zeroText = document.createElementNS("http://www.w3.org/2000/svg", "text");
    zeroText.setAttribute("x", String(padLeft - 8));
    zeroText.setAttribute("y", String(yZero + 3.5));
    zeroText.setAttribute("text-anchor", "end");
    zeroText.setAttribute("fill", "var(--fg, #111)");
    zeroText.setAttribute("font-size", "11px");
    zeroText.setAttribute("font-weight", "600");
    zeroText.setAttribute("font-family", 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
    zeroText.textContent = "0.0%";
    gridGroup.append(zeroText);

    svg.append(gridGroup);

    // Bars
    const n = items.length;
    const slotW = plotWidth / n;
    const barW = Math.max(3, Math.min(slotW * 0.72, 34));

    const barsGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    barsGroup.setAttribute("class", "agg-chart-bars");

    const barNodes = [];

    // Step for X-axis labels to avoid crowding
    let labelStep = 1;
    if (n > 50) labelStep = 8;
    else if (n > 26) labelStep = 4;
    else if (n > 14) labelStep = 2;

    const labelsGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    labelsGroup.setAttribute("class", "agg-chart-labels");

    items.forEach((d, i) => {
      const cx = padLeft + (i + 0.5) * slotW;
      const x = cx - barW / 2;
      const val = d.twr ?? 0;
      const isPositive = val >= 0;

      let h = (Math.abs(val) / (yMax - yMin)) * plotHeight;
      h = Math.max(2, h);

      const y = isPositive ? yZero - h : yZero;
      const fill = isPositive ? "var(--gain, #0a7d3c)" : "var(--loss, #b3261e)";

      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "agg-bar");
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(barW));
      rect.setAttribute("height", String(h));
      rect.setAttribute("rx", String(Math.min(3, barW / 2)));
      rect.setAttribute("fill", fill);
      rect.style.transition = "opacity 0.15s ease, filter 0.15s ease";
      barsGroup.append(rect);
      barNodes.push(rect);

      // Hit area for smooth mouse tracking across the entire column slot
      const hit = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      hit.setAttribute("class", "agg-hit-area");
      hit.setAttribute("x", String(padLeft + i * slotW));
      hit.setAttribute("y", String(padTop));
      hit.setAttribute("width", String(slotW));
      hit.setAttribute("height", String(plotHeight));
      hit.setAttribute("fill", "rgba(0, 0, 0, 0.001)");
      hit.setAttribute("pointer-events", "all");
      hit.style.cursor = "pointer";

      function onHover() {
        barNodes.forEach((b, bi) => {
          if (bi === i) {
            b.style.opacity = "1";
            b.style.filter = "brightness(1.22) drop-shadow(0 2px 5px rgba(0,0,0,0.2))";
          } else {
            b.style.opacity = "0.4";
            b.style.filter = "none";
          }
        });

        const toneClass = f.tone(d.twr);
        const toneValClass = f.tone(d.value_eur);

        tooltip.replaceChildren(
          el("div.agg-tip-header", { text: d.label }),
          el(
            "div.agg-tip-row",
            el("span.agg-tip-lbl", { text: "Return:" }),
            el(`span.agg-tip-val.${toneClass}`, { text: f.pct(d.twr, { sign: true }) }),
          ),
          el(
            "div.agg-tip-row",
            el("span.agg-tip-lbl", { text: "Value gain:" }),
            el(`span.agg-tip-val.${toneValClass}`, { text: f.eur(d.value_eur, { sign: true }) }),
          ),
        );
        tooltip.style.display = "block";

        const tipWidth = tooltip.offsetWidth || 140;
        const tipHeight = tooltip.offsetHeight || 60;

        let tipX = cx - tipWidth / 2;
        if (tipX < 8) tipX = 8;
        if (tipX + tipWidth > width - 8) tipX = width - tipWidth - 8;

        let tipY = isPositive ? y - tipHeight - 8 : y + h + 8;
        if (tipY < 4) tipY = y + 8;
        if (tipY + tipHeight > height - 4) tipY = y - tipHeight - 8;

        tooltip.style.left = `${tipX}px`;
        tooltip.style.top = `${tipY}px`;
      }

      function onLeave() {
        barNodes.forEach((b) => {
          b.style.opacity = "1";
          b.style.filter = "none";
        });
        tooltip.style.display = "none";
      }

      hit.addEventListener("pointerenter", onHover);
      hit.addEventListener("pointermove", onHover);
      hit.addEventListener("pointerleave", onLeave);

      rect.setAttribute("pointer-events", "all");
      rect.addEventListener("pointerenter", onHover);
      rect.addEventListener("pointerleave", onLeave);

      barsGroup.append(hit);

      // X Axis label
      const shouldShowLabel = i % labelStep === 0 || i === n - 1;
      if (shouldShowLabel) {
        const lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
        lbl.setAttribute("x", String(cx));
        lbl.setAttribute("y", String(padTop + plotHeight + 18));
        lbl.setAttribute("text-anchor", "middle");
        lbl.setAttribute("fill", "var(--muted, #666)");
        lbl.setAttribute("font-size", "11px");
        lbl.setAttribute("font-family", 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
        lbl.textContent = d.short_label || d.label;
        labelsGroup.append(lbl);
      }
    });

    svg.append(barsGroup);
    svg.append(labelsGroup);
  }

  draw();

  const ro = new ResizeObserver(() => draw());
  ro.observe(container);
}
