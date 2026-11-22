// Modern Web Helper window extras on the Network page: the helper's live state above the
// schema-driven settings, and the editable home page redirect table below them. The table
// is the list setting network.web_proxy_redirects: edits go into the settings draft, so
// the ordinary changes bar applies them (live, no Mac OS restart).

import { h, uid } from './util.js';
import { badge, button, dialog } from './ui.js';
import { store, subscribe } from './store.js';
import { currentValue, setDraft } from './settings.js';

const KEY = 'network.web_proxy_redirects';
const STATE = {
  running: ['On', 'ok'],
  starting: ['Starting…', 'pending'],
  stopping: ['Stopping…', 'pending'],
  off: ['Off', 'off'],
  failed: ['Not working', 'warn'],
  unavailable: ['Not installed', 'off'],
};

function plural(n, one, many) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function statusLine(wp) {
  if (!wp) return h('p', { class: 'muted small' }, 'Checking the Modern Web Helper…');
  const [word, tone] = STATE[wp.state] || [wp.state, 'quiet'];
  let text;
  if (wp.state === 'running') {
    const c = (wp.stats && wp.stats.counts) || {};
    const parts = [];
    if (c.pages) parts.push(plural(c.pages, 'page', 'pages'));
    if (c.images_converted) parts.push(plural(c.images_converted, 'picture converted', 'pictures converted'));
    if (c.downloads) parts.push(plural(c.downloads, 'download', 'downloads'));
    text = parts.length
      ? `Mac OS's browsers open today's web through RetroBoot: ${parts.join(', ')} so far.`
      : "Mac OS's browsers open today's web, including secure sites, through RetroBoot.";
    if (wp.intercepting === false) text = 'Running, but Mac OS’s web traffic is not reaching it. Turn it off and on again.';
  } else if (wp.state === 'off') {
    text = 'Mac OS connects to web sites directly. Most of today’s sites won’t open in its browsers.';
  } else if (wp.state === 'failed') {
    text = wp.error || 'The helper stopped unexpectedly. RetroBoot keeps trying to start it again.';
  } else if (wp.state === 'unavailable') {
    text = 'This RetroBoot does not include the Modern Web Helper.';
  } else {
    text = '';
  }
  return h('div', { class: 'wh-status' }, h('p', { class: 'lead' }, badge(word, tone), ' ', text));
}

function describeTarget(to) {
  return to === 'search' ? 'Your search page' : to;
}

function describeSource(e) {
  if (!e.path) return `${e.host} (whole site)`;
  if (e.path === '/') return `${e.host} (front page)`;
  return `${e.host}${e.path}`;
}

async function editEntry(entry) {
  const ids = { host: uid('rh'), path: uid('rp'), url: uid('ru'), kind: uid('rk') };
  const host = h('input', { type: 'text', id: ids.host, value: entry ? entry.host : '', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'home.netscape.com' });
  const path = h('input', { type: 'text', id: ids.path, value: entry ? entry.path : '', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'Leave empty for the whole site' });
  const isSearch = !!(entry && entry.to === 'search');
  const toSearch = h('input', { type: 'radio', name: ids.kind, id: uid('rs'), checked: isSearch });
  const toUrl = h('input', { type: 'radio', name: ids.kind, id: uid('ro'), checked: !isSearch });
  const url = h('input', { type: 'text', id: ids.url, value: entry && entry.to !== 'search' ? entry.to : 'http://system7today.com/', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
  const err = h('p', { class: 'field-error', hidden: true, role: 'alert' });
  const sync = () => (url.disabled = toSearch.checked);
  toSearch.addEventListener('change', sync);
  toUrl.addEventListener('change', sync);
  sync();
  const content = h(
    'div',
    { class: 'subsection' },
    h('div', { class: 'field' }, h('label', { for: ids.host }, 'Old site'), host),
    h('div', { class: 'field' }, h('label', { for: ids.path }, 'Page (optional; / means only the front page)'), path),
    h('div', { class: 'field' }, h('span', { class: 'setting-label' }, 'Opens'),
      h('label', { class: 'radio', for: toSearch.id }, toSearch, h('span', null, 'Your search page')),
      h('label', { class: 'radio', for: toUrl.id }, toUrl, h('span', null, 'This address:')), url),
    err,
  );
  let result = null;
  const choice = await dialog({
    title: entry ? 'Change Redirect' : 'Add Redirect',
    icon: 'note',
    message: ['When Mac OS’s browser opens the old site, the Modern Web Helper sends it to the new address instead.'],
    content,
    buttons: [
      { label: 'Cancel', value: 'cancel' },
      { label: entry ? 'Change' : 'Add', value: 'ok', variant: 'default', primary: true },
    ],
    onOpen: () => host.focus(),
    validate: () => {
      const hv = host.value.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '').replace(/\.$/, '');
      let pv = path.value.trim();
      if (pv && !pv.startsWith('/')) pv = '/' + pv;
      const tv = toSearch.checked ? 'search' : url.value.trim();
      let msg = null;
      if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/.test(hv)) msg = 'Enter the old site’s name, such as home.netscape.com.';
      else if (/\s/.test(pv)) msg = 'A page path has no spaces.';
      else if (tv !== 'search' && !/^https?:\/\/[A-Za-z0-9.-]+(:\d+)?(\/\S*)?$/.test(tv)) msg = 'The new address starts with http:// or https://.';
      if (msg) {
        err.hidden = false;
        err.textContent = msg;
        return false;
      }
      result = { host: hv, path: pv, to: tv };
      return true;
    },
  });
  return choice === 'ok' ? result : null;
}

/** Extras for the "Modern Web Helper" window: {top, el, update, destroy}. */
export function webHelperExtras() {
  const top = h('div');
  const listEl = h('ul', { class: 'cp-list rd-list', 'aria-label': 'Home page redirects' });
  const countEl = h('span');
  const details = h(
    'details',
    { class: 'disclosure rd-details' },
    h('summary', null, 'Home page redirects ', countEl),
    h('div', { class: 'subsection' },
      h('p', { class: 'muted small' }, 'Old start and search pages built into Mac OS, Internet Explorer and Netscape, and where they open now. Changes apply when you click Apply.'),
      listEl,
      h('div', { class: 'tag-row' },
        button('Add…', { className: 'btn-small', onClick: () => add() }),
        button('Use Defaults', { className: 'btn-small', variant: 'quiet', onClick: () => defaults() }))),
  );
  const el = h('div', { class: 'wh-extras' }, details);

  const entries = () => (Array.isArray(currentValue(KEY)) ? currentValue(KEY) : []);
  const save = (list) => setDraft(KEY, list);

  async function add() {
    const e = await editEntry(null);
    if (!e) return;
    const list = entries().filter((x) => !(x.host === e.host && x.path === e.path));
    save([...list, e]);
  }

  async function change(i) {
    const list = entries();
    const e = await editEntry(list[i]);
    if (!e) return;
    const next = list.slice();
    next[i] = e;
    save(next.filter((x, j) => j === i || !(x.host === e.host && x.path === e.path)));
  }

  function remove(i) {
    save(entries().filter((_, j) => j !== i));
  }

  function defaults() {
    const def = store.settings && store.settings.defs.get(KEY);
    if (def && Array.isArray(def.default)) save(def.default.map((e) => ({ ...e })));
  }

  function renderList() {
    const list = entries();
    const def = store.settings && store.settings.defs.get(KEY);
    const disabled = !!(def && !def.available);
    countEl.textContent = `(${list.length})`;
    listEl.replaceChildren(
      ...(list.length
        ? list.map((e, i) =>
            h(
              'li',
              { class: 'rd-row' },
              h('span', { class: 'rd-from' }, describeSource(e)),
              h('span', { class: 'rd-arrow', 'aria-hidden': 'true' }, '→'),
              h('span', { class: 'rd-to' }, describeTarget(e.to)),
              h('span', { class: 'tag-row' },
                button('Change…', { className: 'btn-small', variant: 'quiet', disabled, ariaLabel: `Change the redirect for ${describeSource(e)}`, onClick: () => change(i) }),
                button('Remove', { className: 'btn-small', variant: 'quiet', disabled, ariaLabel: `Remove the redirect for ${describeSource(e)}`, onClick: () => remove(i) })),
            ),
          )
        : [h('li', { class: 'muted small' }, 'No redirects. Old start pages open as they are.')]),
    );
  }

  function renderStatus() {
    top.replaceChildren(statusLine(store.status && store.status.web_proxy));
  }

  const update = () => {
    renderList();
    renderStatus();
  };
  update();
  const off = subscribe('status', renderStatus);
  return { top, el, update, destroy: off };
}
