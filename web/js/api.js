// JSON client for the hostd management API (docs/ARCHITECTURE.md section 7).
// Same-origin cookies carry the session; mutating requests add the CSRF header.

const MUTATING = new Set(['POST', 'PATCH', 'PUT', 'DELETE']);
const listeners = { unauthorized: new Set(), connectivity: new Set() };

let csrfToken = null;
let reachable = true;
let sessionRefresher = null;

export class ApiError extends Error {
  constructor(status, code, message, fields = null, body = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.body = body;
  }
  get offline() {
    return this.status === 0;
  }
}

/** Messages shown when the server's own message is missing or too technical. */
const FRIENDLY = {
  unreachable: "Can't reach RetroBoot. Check that it's on and that this device is on the same network.",
  timeout: 'RetroBoot took too long to answer. It may be busy; try again in a moment.',
  'not-paired': 'Pair this browser with RetroBoot to continue.',
  'csrf-invalid': "This page's security token expired. Reload the page and try again.",
  'origin-not-allowed': "RetroBoot refused a request that didn't come from its own page.",
  'stale-revision': 'Settings were changed elsewhere since this page loaded them.',
  'capability-missing': "This RetroBoot doesn't have the hardware for that.",
  'guest-not-running': "Mac OS isn't running.",
  'guest-unresponsive': "Mac OS isn't responding.",
  'too-many-attempts': 'Too many attempts. Wait a little, then try again.',
};

const STATUS_TEXT = {
  400: 'RetroBoot rejected the request.',
  403: "This browser isn't allowed to do that.",
  404: "RetroBoot doesn't know that item. It may have been removed.",
  409: 'That conflicts with something RetroBoot is already doing.',
  422: 'Some values were not accepted.',
  429: 'Too many requests. Wait a moment and try again.',
  500: 'RetroBoot hit an internal problem. Collect evidence on the Diagnostics page if it keeps happening.',
  501: "That feature isn't available on this RetroBoot yet.",
  503: 'RetroBoot is busy starting up. Try again in a moment.',
};

export function friendlyMessage(err) {
  if (!err) return 'Something went wrong.';
  if (err instanceof ApiError) {
    if (err.message && err.code !== 'unreachable' && err.code !== 'timeout') return err.message;
    return FRIENDLY[err.code] || STATUS_TEXT[err.status] || err.message || 'Something went wrong.';
  }
  return err.message || String(err);
}

export function setCsrf(token) {
  csrfToken = token || null;
}

/** The session's CSRF token (for uploads that need XMLHttpRequest progress). */
export const csrf = () => csrfToken;

/** Called to refresh the CSRF token after a csrf-invalid response (set by the app). */
export function setSessionRefresher(fn) {
  sessionRefresher = fn;
}

export function on(event, fn) {
  listeners[event].add(fn);
  return () => listeners[event].delete(fn);
}

export const isReachable = () => reachable;

function setReachable(value) {
  if (reachable === value) return;
  reachable = value;
  for (const fn of listeners.connectivity) fn(value);
}

async function parseError(res) {
  let body = null;
  try {
    const ct = res.headers.get('Content-Type') || '';
    body = ct.includes('json') ? await res.json() : { error: { message: (await res.text()).slice(0, 300) } };
  } catch {
    body = null;
  }
  const e = (body && body.error) || {};
  return new ApiError(res.status, e.code || `http-${res.status}`, e.message || STATUS_TEXT[res.status] || `HTTP ${res.status}`, e.fields || null, body);
}

/**
 * Perform an API request.
 * options.raw: return the Response (for images and downloads)
 * options.timeout: milliseconds (default 15 s)
 * options.quiet401: don't broadcast unauthorized (used by session probing)
 */
export async function api(method, path, body, options = {}) {
  const { raw = false, timeout = 15000, signal, quiet401 = false, retried = false } = options;
  const headers = { Accept: raw ? '*/*' : 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (MUTATING.has(method) && csrfToken) headers['X-RetroBoot-CSRF'] = csrfToken;

  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeout);
  const onAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: ctrl.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (signal && signal.aborted && !timedOut) throw err;
    if (!timedOut) setReachable(false);
    throw new ApiError(0, timedOut ? 'timeout' : 'unreachable', '', null, null);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }

  setReachable(true);

  if (res.status === 401) {
    const err = await parseError(res);
    if (!quiet401) for (const fn of listeners.unauthorized) fn(err);
    throw err;
  }
  if (!res.ok) {
    const err = await parseError(res);
    if (err.status === 403 && err.code === 'csrf-invalid' && !retried && sessionRefresher) {
      await sessionRefresher();
      return api(method, path, body, { ...options, retried: true });
    }
    throw err;
  }
  if (raw) return res;
  if (res.status === 204) return null;
  const ct = res.headers.get('Content-Type') || '';
  if (ct.includes('json')) return res.json();
  return res.text();
}

export const get = (path, opts) => api('GET', path, undefined, opts);
export const post = (path, body = {}, opts) => api('POST', path, body, opts);
export const patch = (path, body, opts) => api('PATCH', path, body, opts);
export const del = (path, opts) => api('DELETE', path, undefined, opts);

/**
 * Send a file as the raw request body with upload progress (fetch has none): same session
 * cookie, CSRF header and JSON errors as api(). Resolves with the parsed JSON body.
 */
export function uploadFile(path, file, contentType, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('Content-Type', contentType);
    xhr.setRequestHeader('X-RetroBoot-CSRF', csrfToken || '');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      setReachable(true);
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        const e = (body && body.error) || {};
        reject(new ApiError(xhr.status, e.code || `http-${xhr.status}`, e.message || 'The upload failed.', e.fields || null, body));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, 'unreachable', ''));
    xhr.send(file);
  });
}

/** True when a response body looks like a job object. */
export function asJob(obj) {
  if (!obj || typeof obj !== 'object') return null;
  if (obj.job && typeof obj.job === 'object' && obj.job.id) return obj.job;
  if (obj.id && obj.state && ('kind' in obj || 'progress' in obj)) return obj;
  return null;
}
