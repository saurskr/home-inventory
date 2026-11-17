// Edit tray: a wide panel for editing one thing without leaving the page.
// Pair with components/tray/tray.css. Plain ES module, no dependencies.
//
//   import { openTray } from './components/tray/tray.js';
//
//   const tray = openTray({
//       title: 'Edit INV-000001',
//       variant: 'grow',                     // 'grow' (default) | 'drawer' | 'instant'
//       origin: document.querySelector('#item-panel'),   // 'grow' widens out of this element
//       width: 820,                          // widest it gets, in px
//       confirmClose: () => !dirty || confirm('Discard your changes?'),   // optional guard
//       onClose: () => {},                   // optional
//     },
//     t => {                                 // fill it: called once, before it opens
//       t.actions.append(saveButton, cancelButton);   // top right of the tray
//       t.body.append(formElement);
//     });
//
//   tray.requestClose();            // what Cancel / Esc / a click outside do: asks confirmClose first
//   await tray.close();             // closes now (after the animation); resolves when it is gone
//   tray.close({ immediate: true }) // no animation, for example when the page is about to re-render
//
// Esc and a click on the dimmed page call requestClose(). Tab stays inside the tray, focus moves to the
// first field on open and back to the button that opened it on close. With "reduce motion" on, nothing animates.

export const TRAY_VARIANTS = ['instant', 'drawer', 'grow'];

const MS = 280;
const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const reducedMotion = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

export function openTray({ title = '', variant = 'grow', origin = null, width = 820, label = '', confirmClose = null, onClose = null } = {}, fill = null) {
  if (!TRAY_VARIANTS.includes(variant)) variant = 'grow';
  const opener = document.activeElement;
  const from = origin && origin.isConnected ? origin : null;
  const kind = variant === 'grow' && !from ? 'instant' : variant;   // nothing to grow out of: just appear

  const scrim = document.createElement('div');
  scrim.className = 'tray-scrim';
  const el = document.createElement('aside');
  el.className = 'tray tray--' + kind;
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', label || title || 'Edit');
  const head = document.createElement('div'); head.className = 'tray-head';
  const titleEl = document.createElement('h2'); titleEl.className = 'tray-title'; titleEl.textContent = title;
  const actions = document.createElement('div'); actions.className = 'tray-actions';
  head.append(titleEl, actions);
  const body = document.createElement('div'); body.className = 'tray-body';
  el.append(head, body);
  document.body.append(scrim, el);

  let closed = false;
  let finished;
  const done = new Promise(r => { finished = r; });
  const tray = { el, head, title: titleEl, actions, body, scrim, close, requestClose, done };

  if (fill) fill(tray);                       // the caller fills the tray before it is measured and shown
  show();
  document.addEventListener('keydown', onKey);
  scrim.addEventListener('click', requestClose);
  return tray;

  function place() {
    const vw = innerWidth, vh = innerHeight;
    const o = from ? from.getBoundingClientRect() : null;
    const narrow = vw < 700;                               // on a phone: nearly the full width
    const left = narrow ? 12 : Math.max(12, o ? Math.round(o.left) : 12);
    const w = narrow ? vw - 24 : Math.max(280, Math.min(width, vw - left - 24));
    const top = kind === 'grow' ? Math.max(64, Math.min(Math.round(o.top), vh - 360)) : 64;
    return { o, left, w, top, maxH: vh - top - 20 };
  }

  function show() {
    if (kind === 'drawer') {
      el.style.width = Math.min(width, Math.round(innerWidth * .92)) + 'px';
      void el.offsetWidth;                                  // commit the off-screen start position
      requestAnimationFrame(() => { el.classList.add('is-open'); scrim.classList.add('is-open'); });
    } else {
      const p = place();
      if (kind === 'instant' || reducedMotion()) {
        Object.assign(el.style, { left: p.left + 'px', top: p.top + 'px', width: p.w + 'px', maxHeight: p.maxH + 'px' });
        scrim.style.transition = 'none';
        el.classList.add('is-open'); scrim.classList.add('is-open');
        if (kind === 'grow') el.style.transition = 'none';
      } else {
        // measure the finished size, then start from the origin's own rectangle and widen into it
        Object.assign(el.style, { left: p.left + 'px', top: p.top + 'px', width: p.w + 'px', height: 'auto', maxHeight: p.maxH + 'px', visibility: 'hidden', transition: 'none' });
        const h = Math.min(el.offsetHeight, p.maxH);
        Object.assign(el.style, { left: p.o.left + 'px', top: p.o.top + 'px', width: p.o.width + 'px', height: p.o.height + 'px', visibility: '' });
        void el.offsetWidth;                                // commit the start rectangle with no transition...
        el.style.transition = '';                           // ...and only then switch the transitions back on
        void el.offsetWidth;
        requestAnimationFrame(() => {
          el.classList.add('is-open'); scrim.classList.add('is-open');
          Object.assign(el.style, { left: p.left + 'px', top: p.top + 'px', width: p.w + 'px', height: h + 'px' });
        });
        setTimeout(() => { if (!closed) el.style.height = 'auto'; }, MS + 40);   // then follow the content
      }
    }
    (body.querySelector(FOCUSABLE) || el).focus({ preventScroll: true });
  }

  function requestClose() {
    if (closed) return false;
    if (confirmClose && !confirmClose()) return false;
    close();
    return true;
  }

  function close({ immediate = false } = {}) {
    if (closed) return done;
    closed = true;
    document.removeEventListener('keydown', onKey);
    const finish = () => {
      el.remove(); scrim.remove();
      if (opener && opener.isConnected && opener !== document.body) opener.focus({ preventScroll: true });
      if (onClose) onClose();
      finished();
    };
    if (immediate || kind === 'instant' || reducedMotion()) { finish(); return done; }
    scrim.classList.remove('is-open');
    if (kind === 'drawer') { el.classList.remove('is-open'); setTimeout(finish, MS); return done; }
    const o = from && from.isConnected ? from.getBoundingClientRect() : null;   // fold back into where it came from
    if (!o) { el.classList.remove('is-open'); setTimeout(finish, 200); return done; }
    el.style.height = el.offsetHeight + 'px';
    void el.offsetWidth;
    el.classList.remove('is-open'); el.classList.add('is-closing');
    Object.assign(el.style, { left: o.left + 'px', top: o.top + 'px', width: o.width + 'px', height: o.height + 'px' });
    setTimeout(finish, MS + 20);
    return done;
  }

  function onKey(e) {
    if (closed) return;
    if (e.key === 'Escape') { e.preventDefault(); requestClose(); return; }
    if (e.key !== 'Tab') return;
    const items = [...el.querySelectorAll(FOCUSABLE)].filter(x => x.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1], at = document.activeElement;
    if (!el.contains(at)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && at === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
  }
}
