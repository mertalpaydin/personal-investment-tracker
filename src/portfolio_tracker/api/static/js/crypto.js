/*
 * Browser-side Web Crypto API decryption for static GitHub Pages deployments.
 *
 * Uses native PBKDF2-HMAC-SHA256 key derivation, AES-256-GCM authenticated
 * decryption, and DecompressionStream("gzip") with zero external libraries.
 */

const TOKEN_STORAGE_KEY = "pt_access_token";

function base64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/**
 * Extract `#token=...` or `?token=...` inside the hash if present, persist it,
 * and clean the hash so the token does not remain visible in the address bar.
 */
export function consumeUrlToken() {
  const hash = window.location.hash || "";
  if (!hash.includes("token=")) return null;

  // Support both `#token=xyz` and `#/overview?token=xyz`
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  if (raw.startsWith("token=")) {
    const token = decodeURIComponent(raw.slice("token=".length).split("&")[0]).trim();
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#/`);
      return token;
    }
  }

  const qIdx = raw.indexOf("?");
  if (qIdx !== -1) {
    const routePart = raw.slice(0, qIdx) || "/";
    const params = new URLSearchParams(raw.slice(qIdx + 1));
    const token = (params.get("token") || "").trim();
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
      params.delete("token");
      const rest = params.toString();
      const nextHash = rest ? `#${routePart}?${rest}` : `#${routePart}`;
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
      return token;
    }
  }
  return null;
}

export function getSavedToken() {
  const fromUrl = consumeUrlToken();
  if (fromUrl) return fromUrl;
  return (
    localStorage.getItem(TOKEN_STORAGE_KEY) ||
    sessionStorage.getItem(TOKEN_STORAGE_KEY) ||
    ""
  );
}

export function saveToken(token, remember = true) {
  const cleaned = String(token || "").trim();
  if (!cleaned) return;
  if (remember) {
    localStorage.setItem(TOKEN_STORAGE_KEY, cleaned);
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } else {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, cleaned);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export function clearToken() {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  sessionStorage.removeItem(TOKEN_STORAGE_KEY);
}

/**
 * Decrypt an AES-256-GCM snapshot envelope using the supplied token.
 */
export async function decryptEnvelope(envelope, token) {
  const cleaned = String(token || "").trim();
  if (!cleaned) {
    throw new Error("Please enter your access token.");
  }
  if (!window.crypto || !window.crypto.subtle) {
    throw new Error("Web Crypto API is unavailable. Open this page over HTTPS or localhost.");
  }

  const salt = base64ToBytes(envelope.salt);
  const iv = base64ToBytes(envelope.iv);
  const ciphertext = base64ToBytes(envelope.ct);
  const iterations = Number(envelope.iter) || 600000;

  const encoder = new TextEncoder();
  const baseKey = await window.crypto.subtle.importKey(
    "raw",
    encoder.encode(cleaned),
    "PBKDF2",
    false,
    ["deriveKey"],
  );

  const aesKey = await window.crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      salt,
      iterations,
      hash: "SHA-256",
    },
    baseKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["decrypt"],
  );

  let decryptedBuffer;
  try {
    decryptedBuffer = await window.crypto.subtle.decrypt(
      { name: "AES-GCM", iv },
      aesKey,
      ciphertext,
    );
  } catch {
    throw new Error("Invalid access token. Could not decrypt portfolio snapshot.");
  }

  if (envelope.compression === "gzip") {
    if (typeof DecompressionStream === "undefined") {
      throw new Error("This browser does not support gzip DecompressionStream.");
    }
    const stream = new Blob([decryptedBuffer])
      .stream()
      .pipeThrough(new DecompressionStream("gzip"));
    return await new Response(stream).json();
  }

  const text = new TextDecoder().decode(decryptedBuffer);
  return JSON.parse(text);
}
