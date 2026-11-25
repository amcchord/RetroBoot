import { post, friendlyMessage } from '../api.js';
import { h, Scope } from '../util.js';
import { pageHeader, notice, button, toast } from '../ui.js';
import { store, subscribe } from '../store.js';
import { settingsPanels, currentValue } from '../settings.js';
import { hw } from '../words.js';

export const meta = { title: 'Sound', icon: 'sound' };

export function render() {
  const scope = new Scope();
  const capEl = h('div', { class: 'notices' });
  let testing = false;
  let testBtn = null;

  const audioPresent = () => {
    const caps = store.settings && store.settings.capabilities;
    if (!caps) return true;
    const outCap = caps.get('audio_output');
    if (outCap) return outCap.present !== false;
    const cap = caps.get('audio');
    return !cap || cap.present !== false;
  };

  const syncTestBtn = () => {
    if (!testBtn) return;
    testBtn.disabled = testing || !audioPresent();
    testBtn.querySelector('.btn-label').textContent = testing ? 'Playing…' : 'Test Sound';
  };

  async function testSound() {
    if (testing) return;
    testing = true;
    syncTestBtn();
    try {
      const output = currentValue('audio.output');
      const body = output ? { output } : {};
      const res = await post('/api/v1/audio/test', body);
      if (res && res.ok === false) {
        toast(res.error || 'Could not play test sound on RetroBoot.', { level: 'error' });
      } else {
        const where = res && res.device ? ` (${res.device})` : '';
        toast(`Played test sound on RetroBoot${where}.`, { level: 'ok', timeout: 3000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      testing = false;
      syncTestBtn();
    }
  }

  const panels = scope.add(
    settingsPanels('sound', {
      extras: {
        Sound: () => {
          testBtn = button('Test Sound', { icon: 'play', className: 'btn-small', focusKey: 'sound-test', onClick: testSound });
          const row = h('div', { class: 'row-actions' }, testBtn, h('span', { class: 'muted small' }, `Plays the selected startup chime through ${hw().sound}.`));
          syncTestBtn();
          return { el: row, update: syncTestBtn };
        },
      },
    }),
  );

  const el = h(
    'div',
    { class: 'page page-narrow' },
    pageHeader('sound', 'Sound', `Mac OS sound plays through ${hw().sound}.`),
    capEl,
    panels.el,
  );
  const update = () => {
    const m = store.settings;
    const cap = m && (m.capabilities.get('audio_output') || m.capabilities.get('audio'));
    capEl.replaceChildren();
    if (cap && !cap.present) capEl.append(notice({ level: 'info', title: 'Sound is not available', message: cap.reason || 'This RetroBoot has no supported sound output.' }));
    syncTestBtn();
  };
  update();
  scope.add(subscribe('settings', update));
  return { el, destroy: () => scope.destroy() };
}
