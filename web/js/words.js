// What to call the computer RetroBoot runs on.  hostd reports host.form_factor
// (and the session's form_factor before pairing): "portable" when the machine has
// a system battery or a portable chassis type, otherwise "desktop".  Only a
// portable is "the laptop"; a mini PC or desktop is "the computer", and its
// screen is just "the display".

import { store } from './store.js';

export function formFactor() {
  return (store.status && store.status.host && store.status.host.form_factor) || (store.session && store.session.form_factor) || null;
}

export function portable() {
  return formFactor() === 'portable';
}

/** Words for the RetroBoot computer, for the form factor known now. */
export function hw() {
  return portable()
    ? {
        the: 'the laptop', The: 'The laptop', its: 'the laptop’s', Title: 'Laptop',
        display: 'laptop display', Display: 'Laptop display', screen: 'laptop screen', Screen: 'Laptop Screen', screenLower: 'Laptop screen',
        sound: 'the laptop’s speakers or headphones',
      }
    : {
        the: 'the computer', The: 'The computer', its: 'the computer’s', Title: 'Computer',
        display: 'display', Display: 'Display', screen: 'screen', Screen: 'Screen', screenLower: 'Screen',
        sound: 'the computer’s speakers, headphones or display',
      };
}
