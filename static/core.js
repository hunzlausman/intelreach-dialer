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
