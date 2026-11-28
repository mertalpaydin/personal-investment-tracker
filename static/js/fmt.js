/*
 * One formatter per kind of number, used everywhere.
 *
 * The rule this file exists to enforce: **null is not zero**. A three-year
 * return that cannot be computed because the instrument has eighteen months of
 * history must render as "—", never as 0.00%. Drawing it as zero says the
 * holding went nowhere, which is a different and false statement.
 *
 * Every function here therefore returns DASH for null/undefined/NaN, and only
 * a real number reaches a numeric formatter.
 */

export const DASH = "—";

const eurFmt = new Intl.NumberFormat("en-IE", {
  style: "currency",
  currency: "EUR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const eurCompact = new Intl.NumberFormat("en-IE", {
  style: "currency",
  currency: "EUR",
  maximumFractionDigits: 0,
});

const numFmt = new Intl.NumberFormat("en-IE", { maximumFractionDigits: 2 });

const dateFmt = new Intl.DateTimeFormat("en-GB", {
  year: "numeric",
  month: "short",
  day: "numeric",
});

const monthFmt = new Intl.DateTimeFormat("en-GB", { year: "numeric", month: "short" });

/** True for anything that can be rendered as a number. */
export function has(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

/** Coerce to a JS number, or null. The API sends floats; a few fields are strings. */
export function num(value) {
  return has(value) ? Number(value) : null;
}

export function eur(value, { sign = false, compact = false } = {}) {
  const v = num(value);
  if (v === null) return DASH;
  const text = (compact ? eurCompact : eurFmt).format(v);
  return sign && v > 0 ? `+${text}` : text;
}

/**
 * An amount in whatever currency it was dealt in.
 *
 * Separate from `eur()` because a lira figure shown with a euro sign is not a
 * formatting slip, it is a wrong number by a factor of forty. Falls back to
 * the code when a currency has no symbol the browser knows.
 */
export function money(value, currency) {
  const v = num(value);
  if (v === null) return DASH;
  if (!currency) return numFmt.format(v);
  try {
    return new Intl.NumberFormat("en-IE", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(v);
  } catch {
    return `${numFmt.format(v)} ${currency}`;
  }
}

export function pct(value, { digits = 2, sign = false } = {}) {
  const v = num(value);
  if (v === null) return DASH;
  const text = `${(v * 100).toFixed(digits)}%`;
  return sign && v > 0 ? `+${text}` : text;
}

/** Percentage *points*, for contributions that already are a difference. */
export function pp(value, { digits = 2 } = {}) {
  const v = num(value);
  if (v === null) return DASH;
  return `${v > 0 ? "+" : ""}${(v * 100).toFixed(digits)} pp`;
}

/** Basis points, as funds quote fees. */
export function bps(value) {
  const v = num(value);
  if (v === null) return DASH;
  return `${(v / 100).toFixed(2)}%`;
}

/** A share quantity. Funds trade in fractions; trailing zeros are noise. */
export function qty(value) {
  const v = num(value);
  if (v === null) return DASH;
  return numFmt.format(v);
}

export function date(value) {
  if (!value) return DASH;
  const d = new Date(`${String(value).slice(0, 10)}T00:00:00`);
  return Number.isNaN(d.getTime()) ? DASH : dateFmt.format(d);
}

export function month(value) {
  if (!value) return DASH;
  const d = new Date(`${String(value).slice(0, 7)}-01T00:00:00`);
  return Number.isNaN(d.getTime()) ? DASH : monthFmt.format(d);
}

/** "3 days ago" / "today". For staleness, where the gap matters more than the date. */
export function ago(value) {
  if (!value) return DASH;
  const then = new Date(`${String(value).slice(0, 10)}T00:00:00`);
  if (Number.isNaN(then.getTime())) return DASH;
  const today = new Date();
  const days = Math.round((today.setHours(0, 0, 0, 0) - then.getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

/** "gain" / "loss" / "" — the class name that colours a figure. */
export function tone(value) {
  const v = num(value);
  if (v === null || v === 0) return "";
  return v > 0 ? "gain" : "loss";
}

/** ISO date string from a Date, for API query parameters. */
export function iso(d) {
  return d.toISOString().slice(0, 10);
}
