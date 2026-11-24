// Normalizes GET /api/v1/settings into one predictable model, tolerating
// small shape differences (list vs. map schema, bare vs. labeled choices,
// range as object or pair). See web/README.md "API contract".

import { sameValue } from './util.js';

/** Which page and group each key prefix belongs to. Unknown prefixes land on Machine & Startup. */
export const GROUPS = {
  machine: { page: 'machine', label: 'Machine' },
  startup: { page: 'machine', label: 'Startup' },
  system: { page: 'machine', label: 'Date & Time' },
  display: { page: 'display', label: 'Display' },
  input: { page: 'display', label: 'Keyboard and Pointer' },
  audio: { page: 'sound', label: 'Sound' },
  catalog: { page: 'storage', label: 'Image Servers' },
  share: { page: 'storage', label: 'Shared Volume' },
  printer: { page: 'storage', label: 'Shared Volume' },
  usb_share: { page: 'storage', label: 'Shared Volume' },
  media: { page: 'storage', label: 'CD-ROM' },
  bluetooth: { page: 'network', label: 'Bluetooth' },
  network: { page: 'network', label: 'Mac OS Networking' },
  management: { page: 'network', label: 'Web Management' },
  remote: { page: 'network', label: 'Remote Screen' },
  update: { page: 'update', label: 'Server Address' },
};

export const PAGE_TITLES = {
  machine: 'Machine & Startup',
  display: 'Display & Input',
  sound: 'Sound',
  storage: 'Storage & Media',
  network: 'Network',
};

/** Keys with their own window inside a page (longest matching key prefix wins). */
export const SUBGROUPS = {
  'network.web_proxy': { page: 'network', label: 'Modern Web Helper' },
  'system.battery_passthrough': { page: 'machine', label: 'Battery' },
};

function subgroup(key) {
  const k = String(key);
  for (const [p, g] of Object.entries(SUBGROUPS)) if (k === p || k.startsWith(p + '_')) return g;
  return null;
}

export function groupOf(key) {
  const prefix = String(key).split('.')[0];
  return subgroup(key) || GROUPS[prefix] || { page: 'machine', label: 'Other Settings' };
}

/** Window title for a setting: ours for known sections (they fit the page), else hostd's group. */
export function groupLabel(key, def) {
  const sub = subgroup(key);
  if (sub) return sub.label;
  const prefix = String(key).split('.')[0];
  return (GROUPS[prefix] && GROUPS[prefix].label) || (def && def.group) || 'Other Settings';
}

const APPLY = {
  live: 'live',
  immediate: 'live',
  guest_restart: 'guest_restart',
  restart: 'guest_restart',
  'guest-restart': 'guest_restart',
  host_reboot: 'host_reboot',
  reboot: 'host_reboot',
  'host-reboot': 'host_reboot',
};

function normalizeType(t) {
  const s = String(t || 'string').toLowerCase();
  if (s === 'boolean' || s === 'bool') return 'bool';
  if (s === 'integer' || s === 'int') return 'int';
  if (s === 'float' || s === 'number' || s === 'real' || s === 'double') return 'number';
  if (s === 'choice' || s === 'select' || s === 'enum') return 'enum';
  if (s === 'list' || s === 'array') return 'list'; // edited by a custom control (e.g. webhelper.js)
  return 'string';
}

function defaultChoiceLabel(value, unit) {
  if (typeof value === 'string' && /^\d+x\d+(@\d+)?$/.test(value)) {
    const [size, rate] = value.split('@');
    return size.replace('x', ' × ') + (rate ? `, ${rate} Hz` : '');
  }
  if (typeof value === 'number' && unit) return `${value} ${unit}`;
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  return String(value);
}

export function normalizeCaps(raw) {
  const caps = new Map();
  if (!raw) return caps;
  if (Array.isArray(raw)) {
    for (const name of raw) caps.set(String(name), { present: true, reason: null });
    return caps;
  }
  for (const [name, v] of Object.entries(raw)) {
    if (v && typeof v === 'object') caps.set(name, { present: v.present !== false && v.available !== false, reason: v.reason || null });
    else caps.set(name, { present: !!v, reason: null });
  }
  return caps;
}

function normalizeDef(d, caps, capsKnown) {
  let type = normalizeType(d.type);
  const unit = d.unit || '';
  let choices = null;
  const rawChoices = d.choices || d.values || d.options;
  if (Array.isArray(rawChoices)) {
    choices = rawChoices.map((c) =>
      c && typeof c === 'object'
        ? { value: c.value !== undefined ? c.value : c.id, label: c.label != null ? String(c.label) : defaultChoiceLabel(c.value !== undefined ? c.value : c.id, unit), help: c.help || null, disabled: !!c.disabled, family: c.family || null, guest_os: c.guest_os || null }
        : { value: c, label: defaultChoiceLabel(c, unit), help: null, disabled: false },
    );
    if (type !== 'bool') type = 'enum';
  }
  let range = null;
  const r = d.range;
  if (Array.isArray(r) && r.length >= 2) range = { min: Number(r[0]), max: Number(r[1]), step: d.step != null ? Number(d.step) : null };
  else if (r && typeof r === 'object') range = { min: Number(r.min), max: Number(r.max), step: r.step != null ? Number(r.step) : d.step != null ? Number(d.step) : null };
  else if (d.min != null && d.max != null) range = { min: Number(d.min), max: Number(d.max), step: d.step != null ? Number(d.step) : null };
  if (range && range.step == null) range.step = type === 'int' ? 1 : (range.max - range.min) / 100;

  const capability = d.capability || null;
  let available = d.available !== false;
  let reason = d.unavailable_reason || null;
  if (capability) {
    const cap = caps.get(capability);
    if (cap ? !cap.present : capsKnown) {
      available = false;
      reason = reason || (cap && cap.reason) || "This RetroBoot doesn't have the hardware this setting needs.";
    }
  }
  if (!available && !reason) reason = "Not available on this RetroBoot.";
  return {
    key: d.key,
    type,
    label: d.label || d.key,
    help: d.help || '',
    apply: APPLY[d.apply] || 'live',
    confirm: d.confirm ? Number(d.confirm) : null,
    capability,
    available,
    reason,
    choices,
    range,
    unit,
    pattern: d.pattern || null,
    minLength: d.min_length != null ? d.min_length : d.minLength != null ? d.minLength : null,
    maxLength: d.max_length != null ? d.max_length : d.maxLength != null ? d.maxLength : null,
    default: d.default,
    group: d.group || null,
    warningValues: d.warning_values || null,
    advanced: !!d.advanced,
    // The kinds of Mac a setting applies to (hostd schema "families"); null: every Mac.
    families: Array.isArray(d.families) ? d.families.map(String) : null,
  };
}

/** The kind of Mac ("powermac", "quadra800") of a machine id, from machine.active's choices. */
export function machineFamily(defs, id) {
  const def = defs && defs.get('machine.active');
  const c = def && def.choices && def.choices.find((x) => x.value === id);
  return (c && c.family) || 'powermac';
}

export const FAMILY_TEXT = {
  powermac: { short: 'PowerPC', cpu: 'PowerPC G4 or G3', machine: 'Power Mac G4' },
  quadra800: { short: '68k', cpu: 'Motorola 68040', machine: 'Quadra 800' },
};

function schemaList(raw) {
  const s = raw.schema !== undefined ? raw.schema : raw.settings;
  if (Array.isArray(s)) return s;
  if (s && typeof s === 'object') {
    if (Array.isArray(s.settings)) return s.settings;
    return Object.entries(s).map(([key, d]) => ({ key, ...d }));
  }
  return [];
}

function normalizeConfirm(pc, receivedAt) {
  if (!pc) return null;
  const keys = pc.keys || (pc.key ? [pc.key] : []);
  let localDeadline = null;
  const left = pc.seconds_left != null ? pc.seconds_left : pc.remaining_s != null ? pc.remaining_s : null;
  if (left != null) localDeadline = receivedAt + Number(left) * 1000;
  else if (pc.deadline) localDeadline = Date.parse(pc.deadline);
  return {
    keys,
    revision: pc.revision != null ? pc.revision : null,
    localDeadline,
    total: pc.seconds != null ? Number(pc.seconds) : null,
    values: pc.values || {},
    previous: pc.previous || {},
  };
}

export function normalizeSettings(raw, extraCaps) {
  const receivedAt = Date.now();
  const caps = normalizeCaps(raw.capabilities);
  if (extraCaps) for (const [k, v] of normalizeCaps(extraCaps)) if (!caps.has(k)) caps.set(k, v);
  const capsKnown = !!raw.capabilities || !!extraCaps;
  const list = schemaList(raw);
  const defs = new Map();
  for (const d of list) if (d && d.key) defs.set(d.key, normalizeDef(d, caps, capsKnown));
  const desired = raw.desired || {};
  const applied = raw.applied || {};
  const pending = new Set(
    Array.isArray(raw.pending) ? raw.pending : [...defs.keys()].filter((k) => k in applied && !sameValue(desired[k], applied[k])),
  );
  return {
    revision: raw.revision,
    appliedRevision: raw.applied_revision != null ? raw.applied_revision : null,
    defs,
    order: [...defs.keys()],
    desired,
    applied,
    pending,
    pendingConfirm: normalizeConfirm(raw.pending_confirm, receivedAt),
    lastConfirm: raw.last_confirm || null,
    knownGood: raw.known_good || null,
    capabilities: caps,
    receivedAt,
  };
}

export function formatValue(def, value) {
  if (!def) return value == null ? '—' : String(value);
  if (value === undefined || value === null) return 'Not set';
  if (Array.isArray(value)) return value.length === 1 ? '1 entry' : `${value.length} entries`;
  if (def.type === 'bool') return value ? 'On' : 'Off';
  if (def.choices) {
    const c = def.choices.find((x) => sameValue(x.value, value));
    if (c) return c.label;
  }
  if (def.type === 'number' || def.type === 'int') {
    const step = def.range && def.range.step;
    const decimals = step && step < 1 ? Math.min(2, String(step).split('.')[1]?.length || 2) : 0;
    const n = Number(value).toFixed(decimals);
    if (!def.unit) return n;
    return def.unit === '%' || def.unit === '×' ? `${n}${def.unit}` : `${n} ${def.unit}`;
  }
  return String(value);
}

/** Client-side checks mirroring the schema; the server remains authoritative (422). */
export function validateValue(def, value) {
  if (!def) return null;
  if (def.type === 'int' || def.type === 'number') {
    if (value === '' || value == null || isNaN(Number(value))) return 'Enter a number.';
    const v = Number(value);
    if (def.type === 'int' && !Number.isInteger(v)) return 'Enter a whole number.';
    if (def.range && (v < def.range.min || v > def.range.max)) return `Choose a value from ${formatValue(def, def.range.min)} to ${formatValue(def, def.range.max)}.`;
  }
  if (def.type === 'string') {
    const s = String(value == null ? '' : value);
    if (def.minLength != null && s.length < def.minLength) return def.minLength === 1 ? "This can't be empty." : `Use at least ${def.minLength} characters.`;
    if (def.maxLength != null && s.length > def.maxLength) return `Use at most ${def.maxLength} characters.`;
    if (def.pattern) {
      let re = null;
      try {
        re = new RegExp(def.pattern);
      } catch {
        re = null; // Pattern not expressible in JS; the server will check it.
      }
      if (re && !re.test(s)) return def.help || "That value isn't allowed.";
    }
  }
  if (def.type === 'enum' && def.choices && !def.choices.some((c) => sameValue(c.value, value))) return 'Choose one of the options.';
  return null;
}

export const APPLY_TEXT = {
  live: 'Applies right away',
  guest_restart: 'Takes effect when Mac OS restarts',
  host_reboot: 'Takes effect when RetroBoot restarts',
};
