// Shared helpers for every view.
export const $ = (s, el = document) => el.querySelector(s);
export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const session = { me: null, onUnauthorized: () => {} };

export async function api(path, { method = 'GET', body } = {}) {
  const r = await fetch('/api' + path, {
    method, credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) {
    const d = data && data.detail;
    const err = new Error((typeof d === 'string' ? d : d ? JSON.stringify(d) : '') || r.statusText);
    err.status = r.status;
    if (r.status === 401 && path !== '/login') session.onUnauthorized();
    throw err;
  }
  return data;
}

let toastTimer;
export function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 6000 : 3000);
}
export const fail = (e) => toast(e.message || String(e), true);

export function when(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + time;
}
export function dur(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
}

export const STATUS_LABEL = {
  new: 'Starting', dialing: 'Dialling', ringing: 'Ringing', answered: 'Answered', 'no-answer': 'No answer', busy: 'Busy',
  cancelled: 'Cancelled', failed: 'Failed', missed: 'Missed', 'sent-to-ghl': 'Sent to GHL', 'no-agents': 'No agent online',
  'voicemail-dropped': 'Voicemail left',
};
export const OUTCOMES = [['', '— outcome —'], ['interested', 'Interested'], ['not-interested', 'Not interested'],
  ['callback', 'Call back'], ['voicemail', 'Voicemail'], ['no-answer', 'No answer'], ['wrong-number', 'Wrong number'],
  ['sale', 'Sale'], ['other', 'Other'], ['do-not-call', 'Do not call']];
export const outcomeLabel = (v) => (v ? (OUTCOMES.find((o) => o[0] === v) || ['', v])[1] : '');
export const CONTACT_STATUSES = ['new', 'contacted', 'interested', 'customer', 'closed'];
export const options = (list, cur) => list.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(cur ?? '') ? ' selected' : ''}>${esc(l)}</option>`).join('');
export const tags = (text) => String(text || '').split(',').filter((x) => x.trim()).map((x) => `<span class="tag">${esc(x.trim())}</span>`).join('');

export function modal(html, onReady, wide = false) {
  $('#modalCard').innerHTML = html;
  $('#modalCard').classList.toggle('wide', wide);
  $('#modal').hidden = false;
  if (onReady) onReady($('#modalCard'));
}
export function closeModal() { $('#modal').hidden = true; $('#modalCard').innerHTML = ''; }

export function pager(total, st) {
  if (total <= 50) return '';
  return `<div class="pager"><span>${st.offset + 1}–${Math.min(total, st.offset + 50)} of ${total}</span>
    <span><button class="btn small" data-page="-1" ${st.offset === 0 ? 'disabled' : ''}>‹ Prev</button>
    <button class="btn small" data-page="1" ${st.offset + 50 >= total ? 'disabled' : ''}>Next ›</button></span></div>`;
}
export function bindPager(box, st, load) {
  box.querySelectorAll('[data-page]').forEach((b) => {
    b.onclick = () => { st.offset = Math.max(0, st.offset + Number(b.dataset.page) * 50); load(); };
  });
}

// form -> plain object (checkboxes -> true/false)
export function formData(form) {
  const out = {};
  form.querySelectorAll('input, select, textarea').forEach((el) => {
    if (!el.name) return;
    out[el.name] = el.type === 'checkbox' ? el.checked : el.value;
  });
  return out;
}

// ---------------------------------------------------------------- icons ----
const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  contacts: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  calls: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.4 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
  campaigns: '<path d="M3 11l18-8v18L3 13z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  agents: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 17h5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  play: '<path d="M6 4l14 8-14 8z"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>',
};
export const icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg>`;

export function pageHead(title, sub = '', actions = '') {
  return `<div class="page-head"><div class="titles"><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ''}</div>
    ${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
}

// per-viewer conveniences only (theme, dock); never required for the app to work
export const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* blocked */ } },
};
