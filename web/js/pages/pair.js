import { post, friendlyMessage } from '../api.js';
import { h, uid, guessDeviceName } from '../util.js';
import { button, docWindow } from '../ui.js';
import { pixelIcon, wordmark } from '../icons.js';
import { hw } from '../words.js';

export const meta = { title: 'Pair', icon: 'devices' };

function connectionNote() {
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  if (location.protocol === 'https:') {
    return h('p', { class: 'conn conn-ok' }, h('span', { class: 'conn-dot', 'aria-hidden': 'true' }), h('span', null, 'Encrypted connection to ', h('strong', null, location.host), '.'));
  }
  if (local) return h('p', { class: 'conn conn-ok' }, h('span', { class: 'conn-dot', 'aria-hidden': 'true' }), h('span', null, 'Connected directly on this machine (', h('strong', null, location.host), ').'));
  return h('p', { class: 'conn conn-bad', role: 'alert' }, h('strong', null, 'This connection is not encrypted. '), `Open the https:// address shown on ${hw().the} instead.`);
}

function certHelp() {
  return h(
    'details',
    { class: 'disclosure' },
    h('summary', null, 'Why did my browser show a security warning?'),
    h(
      'div',
      { class: 'disclosure-body' },
      h('p', null, 'RetroBoot creates its own certificate the first time it starts, because it runs on your local network without an internet service to vouch for it. Browsers warn about certificates like this. The connection is still encrypted; the question is whether you reached your RetroBoot and not something pretending to be it.'),
      h('p', null, h('strong', null, 'To check: '), `compare the certificate’s SHA-256 fingerprint in your browser with the fingerprint shown on ${hw().its} RetroBoot panel, next to this address and the pairing code. They must match exactly. If they don’t, close this page and don’t enter the code.`),
      h(
        'ul',
        { class: 'steps' },
        h('li', null, h('strong', null, 'Safari (Mac): '), 'on the warning page choose Show Details, then View the certificate; or click the padlock area in the address bar. Scroll to Fingerprints, SHA-256.'),
        h('li', null, h('strong', null, 'Safari (iPhone, iPad): '), 'on the warning page tap Show Details, then View Certificate, and look for SHA-256 under Fingerprints.'),
        h('li', null, h('strong', null, 'Chrome, Edge: '), 'click “Not secure” in the address bar, then Certificate details. The SHA-256 fingerprint is under Fingerprints.'),
        h('li', null, h('strong', null, 'Firefox: '), 'on the warning page choose Advanced, then View Certificate. See Fingerprints, SHA-256.'),
      ),
      h('p', { class: 'muted small' }, 'After you’ve checked once, your browser may remember the exception for this address.'),
    ),
  );
}

/** Pairing screen. onPaired(sessionPatch) is called after success. */
export function render({ onPaired, message }) {
  const codeId = uid('code');
  const nameId = uid('name');
  const errId = uid('err');
  const code = h('input', {
    id: codeId,
    class: 'code-input',
    type: 'text',
    inputmode: 'numeric',
    autocomplete: 'one-time-code',
    pattern: '[0-9 ]*',
    maxlength: '7',
    required: true,
    'aria-describedby': `${errId} ${codeId}-hint`,
    placeholder: '000 000',
    oninput: (e) => {
      const digits = e.target.value.replace(/\D/g, '').slice(0, 6);
      e.target.value = digits.length > 3 ? `${digits.slice(0, 3)} ${digits.slice(3)}` : digits;
      clearError();
    },
  });
  const name = h('input', { id: nameId, type: 'text', maxlength: '40', value: guessDeviceName(), autocomplete: 'off', required: true, oninput: clearError });
  const err = h('p', { id: errId, class: 'field-error', role: 'alert', hidden: true });
  const submit = button('Pair', { type: 'submit', variant: 'default' });
  let busy = false;

  function clearError() {
    err.hidden = true;
    code.removeAttribute('aria-invalid');
    name.removeAttribute('aria-invalid');
  }
  function showError(text, field) {
    err.textContent = text;
    err.hidden = false;
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.focus();
      field.select && field.select();
    }
    form.classList.remove('shake');
    void form.offsetWidth;
    form.classList.add('shake');
  }

  const form = h(
    'form',
    {
      class: 'pair-form',
      novalidate: true,
      onsubmit: async (e) => {
        e.preventDefault();
        if (busy) return;
        const digits = code.value.replace(/\D/g, '');
        if (digits.length !== 6) return showError(`Enter all 6 digits of the code shown on ${hw().the}.`, code);
        if (!name.value.trim()) return showError('Give this device a name so you can recognize it later.', name);
        busy = true;
        submit.disabled = true;
        submit.querySelector('.btn-label').textContent = 'Pairing…';
        try {
          const res = await post('/api/v1/pair', { code: digits, device_name: name.value.trim() });
          onPaired(res || {});
        } catch (ex) {
          const f = ex.fields || {};
          if (ex.status === 0) showError(friendlyMessage(ex), null);
          else if (f.device_name && !f.code) showError(f.device_name, name);
          else if (ex.status === 429) showError(ex.message || `Too many attempts. Show a new code on ${hw().the}, then try again.`, code);
          else showError(ex.message || f.code || friendlyMessage(ex), code);
        } finally {
          busy = false;
          submit.disabled = false;
          submit.querySelector('.btn-label').textContent = 'Pair';
        }
      },
    },
    h('div', { class: 'field' }, h('label', { for: codeId }, 'Pairing code'), code, h('p', { class: 'hint', id: `${codeId}-hint` }, `On ${hw().the}, press Ctrl+Alt+Delete and open Network & Web Access (the installer shows it on its Network step). Codes change every 5 minutes.`)),
    h('div', { class: 'field' }, h('label', { for: nameId }, 'Name this device'), name, h('p', { class: 'hint' }, 'Helps you recognize it later in Paired Devices.')),
    err,
    h('div', { class: 'pair-buttons' }, submit),
  );

  const el = h(
    'div',
    { class: 'pair-screen' },
    h(
      'main',
      { id: 'main', class: 'pair-main', tabindex: '-1' },
      docWindow(
        { title: h('h1', { id: 'pair-title' }, 'Pair with RetroBoot'), className: 'pair-win' },
        h('div', { class: 'pair-brand' }, h('span', { html: pixelIcon('mac', 32) }), h('p', { class: 'pair-intro' }, `To manage this RetroBoot from this browser, enter the 6‑digit code shown on ${hw().the}.`)),
        message ? h('p', { class: 'pair-message', role: 'status' }, message) : null,
        connectionNote(),
        form,
        certHelp(),
      ),
    ),
  );
  setTimeout(() => code.focus(), 50);
  return { el, destroy() {} };
}
