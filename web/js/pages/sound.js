import { h, Scope } from '../util.js';
import { pageHeader, notice } from '../ui.js';
import { store, subscribe } from '../store.js';
import { settingsPanels } from '../settings.js';
import { hw } from '../words.js';

export const meta = { title: 'Sound', icon: 'sound' };

export function render() {
  const scope = new Scope();
  const capEl = h('div', { class: 'notices' });
  const panels = scope.add(settingsPanels('sound'));
  const el = h(
    'div',
    { class: 'page page-narrow' },
    pageHeader('sound', 'Sound', `Mac OS sound plays through ${hw().sound}.`),
    capEl,
    panels.el,
  );
  const update = () => {
    const m = store.settings;
    const cap = m && m.capabilities.get('audio');
    capEl.replaceChildren();
    if (cap && !cap.present) capEl.append(notice({ level: 'info', title: 'Sound is not available', message: cap.reason || 'This RetroBoot has no supported sound output.' }));
  };
  update();
  scope.add(subscribe('settings', update));
  return { el, destroy: () => scope.destroy() };
}
