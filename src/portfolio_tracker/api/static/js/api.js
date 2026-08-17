/*
 * The fetch wrapper.
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

function url(path, params) {
  const u = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined || value === "") continue;
    u.searchParams.set(key, String(value));
  }
  return u;
}

async function request(method, path, { params = null, body = null } = {}) {
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
  } catch (cause) {
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
