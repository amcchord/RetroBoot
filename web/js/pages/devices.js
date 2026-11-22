import { get, del, post, friendlyMessage } from '../api.js';
import { h, swap, Scope, fmtDateTime, relTime, describeUserAgent } from '../util.js';
import { win, button, badge, pageHeader, loading, errorState, emptyState, confirm, toast } from '../ui.js';
import { pixelIcon } from '../icons.js';
import { store } from '../store.js';
import { hw } from '../words.js';

export const meta = { title: 'Paired Devices', icon: 'devices' };

export function render(ctx) {
  const scope = new Scope();
  const listEl = h('div', null, loading());
  let devices = null;

  async function load() {
    try {
      const res = await get('/api/v1/devices');
      devices = Array.isArray(res) ? res : res.devices || [];
      paint();
    } catch (err) {
      if (err.status !== 401) swap(listEl, errorState(err, load));
    }
  }

  async function signOut() {
    const ok = await confirm({ title: 'Sign Out of This Browser?', icon: 'caution', message: [`This browser will need a new pairing code from ${hw().the} to manage RetroBoot again.`], confirmLabel: 'Sign Out' });
    if (!ok) return;
    try {
      await post('/api/v1/logout', {});
    } catch (err) {
      if (err.status !== 401) {
        toast(friendlyMessage(err), { level: 'error' });
        return;
      }
    }
    ctx.signedOut(`You signed out. Pair again with a code from ${hw().the} to continue.`);
  }

  async function revoke(d) {
    const ok = await confirm({
      title: `Remove “${d.name}”?`,
      icon: 'caution',
      message: [`${d.name} will be signed out at once and can’t manage RetroBoot until it is paired again with a new code from ${hw().the}.`],
      confirmLabel: 'Remove Device',
      danger: false,
    });
    if (!ok) return;
    try {
      await del(`/api/v1/devices/${encodeURIComponent(d.id)}`);
      toast(`Removed ${d.name}.`, { level: 'ok' });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  function paint() {
    if (!devices.length) return swap(listEl, emptyState('No paired devices.', 'devices'));
    const current = devices.find((d) => d.current) || devices.find((d) => store.session && d.id === store.session.device_id);
    swap(
      listEl,
      h(
        'ul',
        { class: 'device-list' },
        ...devices.map((d) => {
          const mine = d === current;
          return h(
            'li',
            { class: ['device', mine && 'is-current'] },
            h('span', { class: 'device-icon', html: pixelIcon('devices', 32) }),
            h(
              'div',
              { class: 'device-main' },
              h('div', { class: 'machine-head' }, h('strong', null, d.name), mine ? badge('This browser', 'ok') : null),
              h('p', { class: 'muted small' }, `Paired ${fmtDateTime(d.paired_at)}`, d.last_seen ? ` · last active ${mine ? 'now' : relTime(d.last_seen)}` : ''),
              d.user_agent ? h('p', { class: 'muted small' }, describeUserAgent(d.user_agent)) : null,
            ),
            mine
              ? button('Sign Out…', { icon: 'signout', className: 'btn-small', focusKey: 'dev-signout', onClick: signOut })
              : button('Remove…', { className: 'btn-small', focusKey: `dev-${d.id}`, onClick: () => revoke(d) }),
          );
        }),
      ),
    );
  }

  const el = h(
    'div',
    { class: 'page page-narrow' },
    pageHeader('devices', 'Paired Devices', 'Browsers allowed to manage this RetroBoot. Remove any you no longer use.'),
    win({ title: 'Paired Devices', icon: 'devices' }, listEl, h('p', { class: 'muted small' }, `To pair another phone or computer, open RetroBoot Recovery on ${hw().the} and show the web pairing code, then open this page’s address on the new device.`)),
  );
  load();
  return { el, destroy: () => scope.destroy() };
}
