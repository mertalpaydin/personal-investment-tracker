/*
 * The fetch wrapper, supporting both the local FastAPI server and the
 * AES-256-GCM encrypted static snapshot used on GitHub Pages.
 *
 * Two things it does that a bare fetch() does not:
 *
 * 1. Turns a non-2xx into an ApiError carrying the server's own `detail`
 *    string. This backend is unusually good about explaining refusals -- a
 *    missing government base rate returns 409 with a sentence saying so -- and
 *    swallowing that in favour of "Request failed" would throw away the most
 *    useful part of the response.
 *
 * 2. Sends `Cache-Control: no-store`. The figures change when a rebuild runs,
 *    not when a URL changes, so a cached /api/portfolio is a page that quietly
 *    stops updating.
 */

import {
  clearToken,
  decryptEnvelope,
  getSavedToken,
  saveToken,
} from "./crypto.js";

export class ApiError extends Error {
  constructor(message, { status = 0, body = null, url = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.url = url;
  }

  /** 404 here means "nothing built yet", which is a setup state, not a fault. */
  get isMissing() {
    return this.status === 404;
  }

  /** 409 is this API's "I will not guess": a required input is absent. */
  get isRefusal() {
    return this.status === 409;
  }
}

let mode = "unknown"; // "unknown" | "live" | "static"
let encryptedEnvelope = null;
let decryptedSnapshot = null;
let initPromise = null;

export function isStaticMode() {
  return mode === "static";
}

export function isUnlocked() {
  return mode !== "static" || decryptedSnapshot !== null;
}

export function getSnapshotMeta() {
  return decryptedSnapshot?.meta || null;
}

/**
 * Detect whether a static encrypted bundle (`../snapshot.enc.json`) is present.
 * When present, attempts to auto-unlock using a saved token or URL hash token.
 */
export async function initApiMode() {
  if (initPromise) return initPromise;
  initPromise = (async () => {
    try {
      const snapUrl = new URL("../snapshot.enc.json", import.meta.url);
      const resp = await fetch(snapUrl, {
        headers: { Accept: "application/json", "Cache-Control": "no-store" },
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data && data.v === 1 && data.alg === "AES-256-GCM" && data.ct) {
          mode = "static";
          encryptedEnvelope = data;
          const saved = getSavedToken();
          if (saved) {
            try {
              decryptedSnapshot = await decryptEnvelope(encryptedEnvelope, saved);
            } catch {
              clearToken();
              decryptedSnapshot = null;
            }
          }
          return mode;
        }
      }
    } catch {
      // Static bundle not present; use live FastAPI backend.
    }
    mode = "live";
    return mode;
  })();
  return initPromise;
}

export async function unlockSnapshot(token, remember = true) {
  await initApiMode();
  if (!encryptedEnvelope) {
    throw new Error("No encrypted snapshot found.");
  }
  decryptedSnapshot = await decryptEnvelope(encryptedEnvelope, token);
  saveToken(token, remember);
  return decryptedSnapshot.meta || null;
}

export function lockSnapshot() {
  decryptedSnapshot = null;
  clearToken();
}

function normalizeKey(path, params) {
  if (!params) return path;
  const entries = [];
  for (const key of Object.keys(params).sort()) {
    const val = params[key];
    if (val === null || val === undefined || val === "") continue;
    const strVal = typeof val === "boolean" ? (val ? "true" : "false") : String(val);
    entries.push(`${encodeURIComponent(key)}=${encodeURIComponent(strVal)}`);
  }
  return entries.length ? `${path}?${entries.join("&")}` : path;
}

function unwrapEntry(entry, path) {
  if (!entry) {
    throw new ApiError("This figure is not included in the static snapshot.", {
      status: 404,
      url: path,
    });
  }
  if (entry.__error__) {
    throw new ApiError(entry.detail || `HTTP ${entry.status}`, {
      status: entry.status || 400,
      body: entry.body,
      url: path,
    });
  }
  return structuredClone(entry.body);
}

function recalcRebalanceWithInflow(basePlan, cashInflowEur) {
  const plan = structuredClone(basePlan);
  const inflow = Math.max(0, Number(cashInflowEur) || 0);
  const currentTotal = Number(plan.total_current_value_eur) || 0;
  const postTotal = currentTotal + inflow;
  plan.cash_inflow_eur = inflow;
  plan.total_post_value_eur = postTotal;

  // Allocate cash inflow proportionally to positive deficits toward post-inflow targets
  const deficits = plan.buckets.map((b) => {
    const desiredPost = (Number(b.target_weight) || 0) * postTotal;
    return Math.max(0, desiredPost - (Number(b.current_value_eur) || 0));
  });
  const totalDeficit = deficits.reduce((a, b) => a + b, 0);

  const orders = [];
  plan.buckets.forEach((b, idx) => {
    const alloc = totalDeficit > 0 ? (inflow * deficits[idx]) / totalDeficit : 0;
    b.inflow_allocation_eur = Math.round(alloc * 100) / 100;
    b.post_inflow_weight =
      postTotal > 0 ? ((Number(b.current_value_eur) || 0) + b.inflow_allocation_eur) / postTotal : 0;
    if (b.inflow_allocation_eur >= 1 && b.instruments && b.instruments.length > 0) {
      const topInst = b.instruments[0];
      const priceEur = Number(topInst.latest_price_eur) || null;
      orders.push({
        bucket: b.bucket,
        instrument: topInst.instrument,
        name: topInst.name,
        action: "BUY",
        amount_eur: b.inflow_allocation_eur,
        estimated_price_eur: priceEur,
        estimated_quantity: priceEur && priceEur > 0 ? b.inflow_allocation_eur / priceEur : null,
        reason: `Deploy cash inflow into underweight ${b.label}`,
      });
    }
  });
  if (inflow !== Number(basePlan.cash_inflow_eur)) {
    plan.suggested_orders = orders;
  }
  return plan;
}

function serveStaticRequest(method, path, { params = null, body = null } = {}) {
  if (!decryptedSnapshot) {
    throw new ApiError("Portfolio is locked. Enter your access token to view.", {
      status: 401,
      url: path,
    });
  }

  const endpoints = decryptedSnapshot.endpoints || {};

  if (method !== "GET") {
    if (method === "POST" && path === "/api/rebalance/calculate" && body) {
      const presets = unwrapEntry(endpoints["/api/rebalance/presets"], path)?.presets || [];
      const matched = presets.find(
        (p) =>
          p.dimension === body.dimension &&
          JSON.stringify(p.targets) === JSON.stringify(body.targets),
      );
      const presetId = matched ? matched.id : presets[0]?.id;
      const basePlan = decryptedSnapshot.rebalance_presets?.[presetId];
      if (basePlan) {
        return recalcRebalanceWithInflow(basePlan, body.cash_inflow_eur);
      }
    }
    throw new ApiError(
      "Read-only snapshot on GitHub Pages. Run the tracker locally to record trades or change settings.",
      { status: 403, url: path },
    );
  }

  const key = normalizeKey(path, params);
  if (key in endpoints) {
    return unwrapEntry(endpoints[key], path);
  }

  // Dynamic date window slicing for /api/returns/series
  if (path === "/api/returns/series" && "/api/returns/series" in endpoints) {
    const full = unwrapEntry(endpoints["/api/returns/series"], path);
    const since = params?.from ? String(params.from) : null;
    const until = params?.to ? String(params.to) : null;
    if (since) {
      full.points = (full.points || []).filter((p) => p.date >= since);
    }
    if (until) {
      full.points = (full.points || []).filter((p) => p.date <= until);
    }
    if ((since || until) && Array.isArray(full.holdings)) {
      for (const h of full.holdings) {
        if (since) h.points = (h.points || []).filter((p) => p.date >= since);
        if (until) h.points = (h.points || []).filter((p) => p.date <= until);
      }
    }
    return full;
  }

  // Dynamic date window slicing for /api/instruments/:key/series
  if (path.startsWith("/api/instruments/") && path.endsWith("/series") && path in endpoints) {
    const full = unwrapEntry(endpoints[path], path);
    const since = params?.from ? String(params.from) : null;
    const until = params?.to ? String(params.to) : null;
    if (since) full.points = (full.points || []).filter((p) => p.date >= since);
    if (until) full.points = (full.points || []).filter((p) => p.date <= until);
    return full;
  }

  // Dynamic filtering for /api/peers/leaderboard
  if (path === "/api/peers/leaderboard" && "/api/peers/leaderboard?top=500" in endpoints) {
    const full = unwrapEntry(endpoints["/api/peers/leaderboard?top=500"], path);
    let items = full.candidates || [];
    if (params?.risk_level != null && params.risk_level !== "") {
      const lvl = Number(params.risk_level);
      items = items.filter((c) => Number(c.risk_level) === lvl);
    }
    if (params?.risk_bucket) {
      const b = String(params.risk_bucket).toUpperCase();
      items = items.filter((c) => String(c.bucket).toUpperCase() === b);
    }
    if (params?.owned === false || params?.owned === "false") {
      items = items.filter((c) => c.status !== "OWNED");
    }
    const top = params?.top ? Number(params.top) : 500;
    full.candidates = items.slice(0, top);
    full.count = full.candidates.length;
    return full;
  }

  // Dynamic filtering for /api/transactions
  if (path === "/api/transactions" && "/api/transactions?limit=200" in endpoints) {
    const full = unwrapEntry(endpoints["/api/transactions?limit=200"], path);
    let txns = full.transactions || [];
    if (params?.instrument) {
      txns = txns.filter((t) => t.instrument === params.instrument);
    }
    if (params?.type) {
      txns = txns.filter((t) => t.type === params.type);
    }
    if (params?.limit) {
      txns = txns.slice(0, Number(params.limit));
    }
    full.transactions = txns;
    return full;
  }

  // Dynamic filtering for /api/stocks/fundamentals
  if (path === "/api/stocks/fundamentals" && "/api/stocks/fundamentals" in endpoints) {
    const full = unwrapEntry(endpoints["/api/stocks/fundamentals"], path);
    let stocks = full.stocks || [];
    if (params?.sector) {
      stocks = stocks.filter((s) => s.sector === params.sector);
    }
    if (params?.min_score != null && params.min_score !== "") {
      stocks = stocks.filter((s) => (s.value_score ?? -1) >= Number(params.min_score));
    }
    if (params?.max_pe != null && params.max_pe !== "") {
      stocks = stocks.filter(
        (s) => s.forward_pe != null && s.forward_pe > 0 && s.forward_pe <= Number(params.max_pe),
      );
    }
    if (params?.min_fcf_yield != null && params.min_fcf_yield !== "") {
      stocks = stocks.filter(
        (s) => s.fcf_yield != null && s.fcf_yield >= Number(params.min_fcf_yield),
      );
    }
    full.stocks = stocks;
    full.count = stocks.length;
    return full;
  }

  if (path in endpoints) {
    return unwrapEntry(endpoints[path], path);
  }

  throw new ApiError("This endpoint is not available in the static snapshot.", {
    status: 404,
    url: path,
  });
}

function url(path, params) {
  const u = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    u.searchParams.set(key, String(value));
  }
  return u;
}

async function request(method, path, { params = null, body = null } = {}) {
  if (mode === "unknown") {
    await initApiMode();
  }
  if (mode === "static") {
    return serveStaticRequest(method, path, { params, body });
  }

  const target = url(path, params);
  const init = {
    method,
    headers: { Accept: "application/json", "Cache-Control": "no-store" },
  };
  if (body !== null) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(target, init);
  } catch {
    // The server is local, so this is almost always "it stopped".
    throw new ApiError("Could not reach the server. Is it still running?", {
      url: target.pathname,
    });
  }

  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    const detail = parsed && typeof parsed.detail === "string" ? parsed.detail : null;
    throw new ApiError(detail || `${response.status} ${response.statusText}`, {
      status: response.status,
      body: parsed,
      url: target.pathname,
    });
  }
  return parsed;
}

export const get = (path, params) => request("GET", path, { params });
export const post = (path, body, params) => request("POST", path, { body: body ?? {}, params });
export const put = (path, body, params) => request("PUT", path, { body: body ?? {}, params });
export const patch = (path, body, params) => request("PATCH", path, { body: body ?? {}, params });
export const del = (path, params) => request("DELETE", path, { params });

/**
 * Fetch several endpoints at once, tolerating individual failures.
 *
 * Returns `{key: value}` for what succeeded and `{key: ApiError}` for what did
 * not, so one endpoint refusing (no tax constant, no compositions imported)
 * degrades that panel rather than blanking the screen. Callers check with
 * `isError`.
 */
export async function all(spec) {
  const keys = Object.keys(spec);
  const settled = await Promise.allSettled(keys.map((k) => spec[k]));
  const out = {};
  keys.forEach((key, i) => {
    const result = settled[i];
    out[key] = result.status === "fulfilled" ? result.value : result.reason;
  });
  return out;
}

export const isError = (value) => value instanceof Error;
