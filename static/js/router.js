/*
 * A hash router.
 *
 * Hash rather than History API on purpose: `#/holdings/SEC0` needs no
 * server-side catch-all route, cannot shadow `/api/*`, deep-links correctly,
 * and survives a reload with zero backend involvement. For a single-user app
 * served by the same process that owns the database, the tidier-looking URL is
 * not worth a route that has to be kept from swallowing the API.
 */

const routes = [];
let notFound = null;
let current = null;

/**
 * Register a route.
 *
 * @param {string} pattern - e.g. "/", "/holdings", "/holdings/:key"
 * @param {(ctx: {params: object, query: URLSearchParams, path: string}) => Promise<Node>|Node} view
 */
export function route(pattern, view) {
  const names = [];
  const source = pattern
    .split("/")
    .map((part) => {
      if (!part.startsWith(":")) return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      names.push(part.slice(1));
      return "([^/]+)";
    })
    .join("/");
  routes.push({ pattern, regex: new RegExp(`^${source}$`), names, view });
}

export function fallback(view) {
  notFound = view;
}

function parse() {
  const raw = window.location.hash.replace(/^#/, "") || "/";
  const [path, queryString = ""] = raw.split("?");
  return { path: path || "/", query: new URLSearchParams(queryString) };
}

function match(path) {
  for (const entry of routes) {
    const found = entry.regex.exec(path);
    if (!found) continue;
    const params = {};
    entry.names.forEach((name, i) => {
      params[name] = decodeURIComponent(found[i + 1]);
    });
    return { entry, params };
  }
  return null;
}

/** The route pattern currently rendered, for marking the nav. */
export function currentPattern() {
  return current;
}

/** Navigate. Same-path calls still re-render, which is what a refresh means. */
export function go(hash) {
  const target = hash.startsWith("#") ? hash : `#${hash}`;
  if (window.location.hash === target) render();
  else window.location.hash = target;
}

let render = () => {};

/**
 * Start routing.
 *
 * `onRender` is called after each view is mounted, so chrome that depends on
 * the current route (nav highlighting) updates in one place.
 */
export function start({ mount, onError, onRender = null }) {
  let generation = 0;

  render = async () => {
    const { path, query } = parse();
    const found = match(path);
    current = found ? found.entry.pattern : null;

    // A slow view must not overwrite a faster one the user navigated to after
    // it. Only the newest render is allowed to touch the DOM.
    const mine = ++generation;

    try {
      const view = found ? found.entry.view : notFound;
      if (!view) throw new Error(`No view for ${path}`);
      const node = await view({ params: found ? found.params : {}, query, path });
      if (mine !== generation) return;
      mount.replaceChildren(node);
      window.scrollTo(0, 0);
    } catch (error) {
      if (mine !== generation) return;
      mount.replaceChildren(onError(error));
    }
    if (onRender) onRender({ path, pattern: current, query });
  };

  window.addEventListener("hashchange", render);
  if (!window.location.hash) window.location.hash = "#/";
  else render();
}

/** Force the current view to rebuild — after a write, typically. */
export function refresh() {
  return render();
}
