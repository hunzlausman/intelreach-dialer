// IntelReach Calling CRM – app shell, softphone panel, power dialer, contacts and calls.
import { viewAdmin } from './admin.js';
import { viewAgents } from './agents.js';
import { viewCampaign, viewCampaigns } from './campaigns.js';
import { startCaptions, stopCaptions } from './captions.js';
import {
  $, api, bindPager, closeModal, CONTACT_STATUSES, dur, esc, fail, formData, icon, modal, options, OUTCOMES, outcomeLabel,
  pageHead, pager, session, STATUS_LABEL, store, tags, toast, when,
} from './core.js';
import { viewDashboard } from './dashboard.js';
import { Phone } from './phone.js';

$('#modal').addEventListener('mousedown', (e) => { if (e.target.id === 'modal') closeModal(); });

const phone = new Phone($('#remoteAudio'));
window.__crmPhone = phone;   // for debugging in the browser console
let dialNumber = '';
let wrap = null;          // finished call waiting for an outcome { callId, number, name }
let callTimer = null;
let dialer = null;        // power dialer: { campaign, lead, contact, script, history, countdown, timer, message }
let refreshView = () => {};
const ctx = {
  setRefresh: (fn) => { refreshView = fn; },
  startDialing: (c) => startDialing(c),
  renderPhone: () => renderPhone(),
  sub: '',
};

// --------------------------------------------------------------- shell ----
const NAV = [
  ['dashboard', 'Dashboard', 'dashboard'], ['contacts', 'Contacts', 'contacts'], ['calls', 'Calls', 'calls'],
  ['campaigns', 'Campaigns', 'campaigns'], ['agents', 'AI agents', 'agents', true], ['admin', 'Settings', 'settings', true],
];
const TITLES = Object.fromEntries(NAV.map(([v, t]) => [v, t]));

function buildShell() {
  const admin = session.me.user.role === 'admin';
  $('#nav').innerHTML = NAV.filter((n) => !n[3] || admin)
    .map(([v, label, ic]) => `<a class="nav-item" href="#/${v}" data-view="${v}">${icon(ic)}<span>${label}</span></a>`).join('');
  $('#menuBtn').innerHTML = icon('menu');
  $('#logoutBtn').innerHTML = icon('logout');
  $('#fab').innerHTML = icon('calls');
  const u = session.me.user;
  $('#avatar').textContent = (u.name || u.email).split(/\s+/).map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  $('#userRole').textContent = u.role === 'admin' ? 'Admin' : 'Agent';
  themeIcon();
  setDock(store.get('dock') !== 'closed' && window.innerWidth > 1100);
}

function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function themeIcon() { $('#themeBtn').innerHTML = icon(effectiveTheme() === 'dark' ? 'sun' : 'moon'); }
$('#themeBtn').addEventListener('click', () => {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  store.set('theme', next);
  themeIcon();
  refreshView();               // charts re-read their colours
});

function setDock(open) {
  $('#app').classList.toggle('dock-closed', !open);
  if (window.innerWidth > 1100) store.set('dock', open ? null : 'closed');
}
const openDock = () => setDock(true);
$('#dockToggle').addEventListener('click', () => setDock($('#app').classList.contains('dock-closed')));
$('#dockClose').addEventListener('click', () => setDock(false));
$('#fab').addEventListener('click', openDock);
function setNav(open) { $('#app').classList.toggle('nav-open', open); $('#scrim').hidden = !open; }
$('#menuBtn').addEventListener('click', () => setNav(true));
$('#scrim').addEventListener('click', () => setNav(false));
$('#nav').addEventListener('click', (e) => { if (e.target.closest('a')) setNav(false); });

// ---------------------------------------------------------------- auth ----
function showLogin() {
  phone.stop();
  stopDialing(true);
  session.me = null;
  $('#app').hidden = true;
  $('#login').hidden = false;
}
session.onUnauthorized = showLogin;

$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $('#loginError').textContent = '';
  try {
    await api('/login', { method: 'POST', body: { email: f.get('email'), password: f.get('password') } });
    await boot();
  } catch (err) { $('#loginError').textContent = err.message; }
});

$('#logoutBtn').addEventListener('click', async () => {
  if (phone.session && !confirm('You are on a call. Sign out anyway?')) return;
  phone.hangup();
  await api('/logout', { method: 'POST' }).catch(() => {});
  showLogin();
});

async function boot() {
  try { session.me = await api('/me'); } catch { showLogin(); return; }
  const me = session.me;
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#companyName').textContent = me.company;
  document.title = me.company;
  $('#userName').textContent = me.user.name;
  $('#availToggle').checked = !!me.user.available;
  buildShell();
  if (me.sip) phone.start(me.sip);
  renderPhone();
  heartbeat();
  route();
  if ('Notification' in window && Notification.permission === 'default') {
    document.addEventListener('click', () => Notification.requestPermission().catch(() => {}), { once: true });
  }
}

function heartbeat(available) {
  if (!session.me) return;
  api('/me/heartbeat', { method: 'POST', body: { available, registered: phone.state === 'ready' } }).catch(() => {});
}
setInterval(() => heartbeat(), 25000);
$('#availToggle').addEventListener('change', (e) => {
  heartbeat(e.target.checked);
  toast(e.target.checked ? 'You will ring for incoming calls' : 'Incoming calls will not ring here');
});
window.addEventListener('beforeunload', (e) => { if (phone.session) { e.preventDefault(); e.returnValue = ''; } });

// --------------------------------------------------------------- phone ----
const KEYS = [['1', ''], ['2', 'ABC'], ['3', 'DEF'], ['4', 'GHI'], ['5', 'JKL'], ['6', 'MNO'], ['7', 'PQRS'], ['8', 'TUV'], ['9', 'WXYZ'], ['*', ''], ['0', '+'], ['#', '']];
const keypad = () => `<div class="keypad">${KEYS.map(([k, l]) => `<button data-key="${k}">${k}<small>${l}</small></button>`).join('')}</div>`;

async function placeCall(number, contactId = null, name = '', leadId = null) {
  const me = session.me;
  if (!me.sip) return toast('You have no phone line – ask an admin', true);
  if (phone.state !== 'ready') return toast('Phone line is not connected yet', true);
  if (phone.session) return toast('Finish the current call first', true);
  if (wrap) await saveWrap(true);
  try {
    const c = await api('/calls', { method: 'POST', body: { number, contact_id: contactId, lead_id: leadId } });
    if (!name) {
      const l = await api('/lookup?number=' + encodeURIComponent(c.number)).catch(() => null);
      name = (l && l.contact && l.contact.name) || '';
    }
    dialNumber = c.number;
    phone.dial(c.number, { callId: c.id, name });
  } catch (e) {
    fail(e);
    if (dialer && leadId) nextLead();
  }
}

function lineStatus() {
  const me = session.me;
  const st = { ready: ['ready', `Line ${me && me.sip ? me.sip.ext : ''} ready`], connecting: ['connecting', 'Connecting…'],
    error: ['error', 'Phone error'], off: ['', me && !me.sip ? 'No phone line' : 'Phone off'] }[phone.state];
  ['#lineStatus', '#lineStatusM'].forEach((sel) => {
    const line = $(sel);
    line.className = 'pill ' + st[0]; line.textContent = st[1]; line.title = phone.error || '';
  });
  const live = !!(phone.call && phone.session);
  $('#fab').classList.toggle('live', live);
  if (live || (phone.call && phone.call.status === 'ringing')) openDock();     // never miss a call
}

function renderPhone() {
  if (!session.me) return;
  const el = $('#phonePanel');
  const c = phone.call;
  lineStatus();
  clearInterval(callTimer);

  if (c && c.dir === 'in' && c.status === 'ringing') {
    el.innerHTML = `<div class="callcard incoming">
      <div class="state">Incoming call</div>
      <div class="who">${esc(c.name || c.number)}</div>
      <div class="sub">${c.name && c.name !== c.number ? esc(c.number) : ''}<span id="inCompany"></span></div>
      <div class="controls" style="grid-template-columns:1fr 1fr">
        <button class="btn green" id="answerBtn">Answer</button><button class="btn red" id="declineBtn">Decline</button>
      </div></div>`;
    $('#answerBtn').onclick = () => phone.answer();
    $('#declineBtn').onclick = () => phone.hangup();
    api('/lookup?number=' + encodeURIComponent(c.number)).then((l) => {
      if (l.contact && $('#inCompany')) $('#inCompany').innerHTML = (l.contact.company ? ' · ' + esc(l.contact.company) : '') + ` · <a href="#/contacts/${l.contact.id}">open</a>`;
    }).catch(() => {});
    return;
  }

  if (c && phone.session) {
    const state = { calling: 'Calling…', ringing: 'Ringing…', connecting: 'Connecting…', active: c.held ? 'On hold' : 'Connected' }[c.status] || c.status;
    el.innerHTML = `${dialer ? `<div class="dialer-head">${esc(dialer.campaign.name)}</div>` : ''}<div class="callcard">
      <div class="state">${esc(state)}${session.me.recording ? ' · <span class="rec">● REC</span>' : ''}</div>
      <div class="who">${esc(c.name || c.number)}</div>
      <div class="sub">${c.name ? esc(c.number) : ''}</div>
      <div class="timer" id="callTimer">${c.startedAt ? dur((Date.now() - c.startedAt) / 1000) : '0:00'}</div>
      <div class="controls">
        <button class="btn ${c.muted ? 'on' : ''}" id="muteBtn">${c.muted ? 'Unmute' : 'Mute'}</button>
        <button class="btn ${c.held ? 'on' : ''}" id="holdBtn" ${c.status !== 'active' ? 'disabled' : ''}>${c.held ? 'Resume' : 'Hold'}</button>
        <button class="btn" id="padBtn">Keypad</button>
      </div>
      <div id="dtmfPad" hidden>${keypad()}</div>
      <button class="btn red wide" id="hangBtn">Hang up</button></div>
      ${dialer && dialer.script ? `<details class="script" open><summary>Script</summary>${esc(dialer.script).replace(/\n/g, '<br>')}</details>` : ''}`;
    $('#muteBtn').onclick = () => phone.toggleMute();
    $('#holdBtn').onclick = () => phone.toggleHold();
    $('#padBtn').onclick = () => { $('#dtmfPad').hidden = !$('#dtmfPad').hidden; };
    $('#hangBtn').onclick = () => phone.hangup();
    el.querySelectorAll('[data-key]').forEach((b) => { b.onclick = () => phone.dtmf(b.dataset.key); });
    if (c.startedAt) callTimer = setInterval(() => { const t = $('#callTimer'); if (t) t.textContent = dur((Date.now() - c.startedAt) / 1000); }, 1000);
    return;
  }

  const wrapHtml = wrap ? `<div class="panel note" style="margin:0 0 12px">
      <b>Call ended</b> – ${esc(wrap.name || wrap.number)}${wrap.reason ? ` <span class="muted">(${esc(wrap.reason)})</span>` : ''}
      <label style="margin-top:8px">Outcome <select id="wrapOutcome">${options(OUTCOMES, '')}</select></label>
      <label>Notes <textarea id="wrapNotes" placeholder="What was agreed?"></textarea></label>
      <div style="display:flex;gap:8px"><button class="btn primary" id="wrapSave">${dialer ? 'Save & next' : 'Save'}</button>
        <button class="btn ghost" id="wrapSkip">Skip</button></div>
    </div>` : '';

  if (dialer) {
    const d = dialer;
    const lead = d.contact ? `<div class="lead-card">
        <div class="who">${esc(d.contact.name || d.contact.phone)}</div>
        <div class="sub">${esc(d.contact.phone)}${d.contact.company ? ' · ' + esc(d.contact.company) : ''}</div>
        ${d.contact.notes ? `<div class="note" style="margin:6px 0">${esc(d.contact.notes)}</div>` : ''}
        ${d.history && d.history.length ? `<div class="muted small">Last: ${esc(when(d.history[0].started_at))} – ${esc(STATUS_LABEL[d.history[0].status] || d.history[0].status)} ${esc(outcomeLabel(d.history[0].disposition))}</div>` : ''}
        ${d.script ? `<details class="script"><summary>Script</summary>${esc(d.script).replace(/\n/g, '<br>')}</details>` : ''}
        ${!wrap ? `<div class="controls" style="grid-template-columns:2fr 1fr">
          <button class="btn green" id="dlCall">${d.countdown > 0 && !d.paused ? `Calling in ${d.countdown}s – call now` : 'Call now'}</button>
          <button class="btn" id="dlSkip">Skip</button></div>` : ''}
      </div>` : `<div class="empty small">${esc(d.message || 'Getting the next lead…')}
        ${d.message ? '<br><button class="btn small" id="dlRetry" style="margin-top:8px">Check again</button>' : ''}</div>`;
    el.innerHTML = `${wrapHtml}<div class="dialer-head">${esc(d.campaign.name)}
        <span><button class="btn small ghost" id="dlPause">${d.paused ? 'Resume' : 'Pause'}</button><button class="btn small ghost" id="dlStop">Stop</button></span></div>
      ${lead}<div class="phone-foot">${phone.error ? `<span class="error">${esc(phone.error)}</span>` : 'Power dialer'}</div>`;
    if ($('#dlCall')) $('#dlCall').onclick = () => dialLead();
    if ($('#dlSkip')) $('#dlSkip').onclick = async () => {
      clearInterval(d.timer);
      await api(`/campaigns/${d.campaign.id}/skip/${d.lead.id}`, { method: 'POST' }).catch(() => {});
      nextLead();
    };
    if ($('#dlRetry')) $('#dlRetry').onclick = () => nextLead();
    $('#dlPause').onclick = () => { d.paused = !d.paused; if (d.paused) clearInterval(d.timer); else if (d.lead && !wrap) countdown(); renderPhone(); };
    $('#dlStop').onclick = () => stopDialing();
    bindWrap();
    return;
  }

  el.innerHTML = `${wrapHtml}
    <input class="number" id="dialInput" placeholder="+44 20 7946 0958" value="${esc(dialNumber)}" autocomplete="off" inputmode="tel">
    ${keypad()}
    <button class="btn green wide" id="callBtn" ${phone.state !== 'ready' ? 'disabled' : ''}>Call</button>
    <div class="phone-foot">${session.me.callerId ? 'Caller ID ' + esc(session.me.callerId) : 'No caller ID yet (Admin → SIP trunks)'}
      · <a href="#" id="echoBtn">audio test</a>${phone.error ? `<br><span class="error">${esc(phone.error)}</span>` : ''}</div>`;
  const inp = $('#dialInput');
  inp.oninput = () => { dialNumber = inp.value; };
  inp.onkeydown = (e) => { if (e.key === 'Enter') placeCall(inp.value); };
  el.querySelectorAll('[data-key]').forEach((b) => {
    b.onclick = () => { inp.value += b.dataset.key; dialNumber = inp.value; inp.focus(); };
    if (b.dataset.key === '0') b.oncontextmenu = (e) => { e.preventDefault(); inp.value += '+'; dialNumber = inp.value; };
  });
  $('#callBtn').onclick = () => placeCall(inp.value);
  $('#echoBtn').onclick = (e) => {
    e.preventDefault();
    if (phone.state !== 'ready' || phone.session) return;
    try { phone.dial('600', { name: 'Audio test (you should hear yourself)' }); } catch (err) { fail(err); }
  };
  bindWrap();
}

function bindWrap() {
  if (!wrap) return;
  $('#wrapSave').onclick = () => saveWrap();
  $('#wrapSkip').onclick = () => { wrap = null; if (dialer) nextLead(); else renderPhone(); };
}

async function saveWrap(silent = false) {
  if (!wrap) return;
  const outcome = $('#wrapOutcome') ? $('#wrapOutcome').value : '';
  const notes = $('#wrapNotes') ? $('#wrapNotes').value : '';
  const id = wrap.callId;
  wrap = null;
  if (id && (outcome || notes)) {
    try { await api('/calls/' + id, { method: 'PATCH', body: { disposition: outcome, notes } }); if (!silent) toast('Saved'); }
    catch (e) { fail(e); }
  }
  if (dialer && !silent) nextLead(); else renderPhone();
  refreshView();
}

let lastStatus = '';
phone.on(() => {
  const c = phone.call;
  if (c && c.status === 'active' && lastStatus !== 'active' && session.me.liveCaptions && c.callId) {
    startCaptions(phone.session, c.callId).catch(() => {});
  }
  // a call just finished: offer the outcome form (only for real CRM calls)
  if (c && ['ended', 'failed', 'missed'].includes(c.status) && lastStatus !== c.status) {
    stopCaptions();
    if (c.status === 'failed' && c.reason) toast(c.reason, true);
    if (c.callId && (c.status === 'ended' || c.dir === 'out')) wrap = { callId: c.callId, number: c.number, name: c.name, reason: c.status === 'failed' ? c.reason : '' };
    phone.clear();
    setTimeout(() => refreshView(), 1500);   // let Asterisk report the final status first
  }
  lastStatus = c ? c.status : '';
  if (phone.state === 'ready' && !renderPhone.wasReady) heartbeat();
  renderPhone.wasReady = phone.state === 'ready';
  renderPhone();
});

// -------------------------------------------------------- power dialer ----
function startDialing(campaign) {
  if (!session.me.sip) return toast('You have no phone line – ask an admin', true);
  if (phone.session) return toast('Finish the current call first', true);
  stopDialing(true);
  dialer = { campaign: { id: campaign.id, name: campaign.name }, lead: null, contact: null, countdown: 0, timer: null, paused: false };
  toast(`Dialing ${campaign.name}`);
  nextLead();
}

function stopDialing(silent) {
  if (!dialer) return;
  clearInterval(dialer.timer);
  clearTimeout(dialer.retry);
  if (dialer.lead && !phone.session) api(`/campaigns/${dialer.campaign.id}/skip/${dialer.lead.id}`, { method: 'POST' }).catch(() => {});
  dialer = null;
  if (!silent) { toast('Power dialer stopped'); renderPhone(); }
}

async function nextLead() {
  const d = dialer;
  if (!d) return;
  clearInterval(d.timer);
  clearTimeout(d.retry);
  d.lead = null; d.contact = null; d.message = '';
  renderPhone();
  try {
    const r = await api(`/campaigns/${d.campaign.id}/next`, { method: 'POST' });
    if (dialer !== d) return;
    if (!r.lead) {
      d.message = r.message;
      d.retry = setTimeout(nextLead, 30000);
    } else {
      Object.assign(d, { lead: r.lead, contact: r.contact, script: r.script, history: r.history, countdown: r.preview_seconds });
      if (!d.paused) countdown();
    }
  } catch (e) { d.message = e.message; }
  renderPhone();
}

function countdown() {
  const d = dialer;
  clearInterval(d.timer);
  if (d.countdown <= 0) { dialLead(); return; }
  d.timer = setInterval(() => {
    if (dialer !== d || phone.session) { clearInterval(d.timer); return; }
    d.countdown -= 1;
    if (d.countdown <= 0) { clearInterval(d.timer); dialLead(); } else renderPhone();
  }, 1000);
}

function dialLead() {
  const d = dialer;
  if (!d || !d.lead) return;
  clearInterval(d.timer);
  d.countdown = 0;
  placeCall(d.contact.phone, d.contact.id, d.contact.name, d.lead.id);
}

// -------------------------------------------------------------- router ----
window.addEventListener('hashchange', route);

function route() {
  if (!session.me) return;
  let [, view = 'dashboard', id] = location.hash.split('/');
  const admin = session.me.user.role === 'admin';
  if (!TITLES[view] || ((view === 'agents' || view === 'admin') && !admin)) view = 'dashboard';
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  $('#mobileTitle').textContent = TITLES[view];
  const el = $('#view');
  ctx.sub = id || '';
  window.scrollTo(0, 0);
  if (view === 'dashboard') return viewDashboard(el, ctx);
  if (view === 'calls' && id) return viewCallPage(el, Number(id));
  if (view === 'calls') return viewCalls(el);
  if (view === 'campaigns' && id) return viewCampaign(el, Number(id), ctx);
  if (view === 'campaigns') return viewCampaigns(el, ctx);
  if (view === 'agents' && admin) return viewAgents(el, ctx);
  if (view === 'admin' && admin) return viewAdmin(el, ctx);
  if (view === 'contacts' && id) return viewContact(el, Number(id));
  return viewContacts(el);
}

// ------------------------------------------------------------ contacts ----
const contactsState = { q: '', status: '', offset: 0 };

async function viewContacts(el) {
  el.innerHTML = `${pageHead('Contacts', 'Everyone you call or who calls you.',
      '<button class="btn" id="importBtn">Import CSV</button><button class="btn primary" id="newContact">+ Contact</button>')}
    <div class="toolbar">
      <input id="cq" type="search" placeholder="Search name, phone, company, tag…" value="${esc(contactsState.q)}">
      <select id="cstatus" style="max-width:180px"><option value="">All statuses</option>${options(CONTACT_STATUSES.map((s) => [s, s[0].toUpperCase() + s.slice(1)]), contactsState.status)}</select>
    </div>
    <div class="card table-wrap" id="clist"><div class="empty">Loading…</div></div>`;
  let t;
  $('#cq').oninput = (e) => { clearTimeout(t); t = setTimeout(() => { contactsState.q = e.target.value; contactsState.offset = 0; load(); }, 250); };
  $('#cstatus').onchange = (e) => { contactsState.status = e.target.value; contactsState.offset = 0; load(); };
  $('#newContact').onclick = () => editContact(null, load);
  $('#importBtn').onclick = () => importCsv(load);

  async function load() {
    try {
      const qs = new URLSearchParams({ q: contactsState.q, status: contactsState.status, limit: 50, offset: contactsState.offset });
      const d = await api('/contacts?' + qs);
      const box = $('#clist');
      if (!box) return;
      if (!d.items.length) {
        box.innerHTML = contactsState.q ? '<div class="empty"><b>No matches</b>Try another name, number or tag.</div>'
          : '<div class="empty"><b>No contacts yet</b>Add one, or import a CSV with a phone column.</div>';
        return;
      }
      box.innerHTML = `<table><thead><tr><th>Name</th><th>Phone</th><th>Company</th><th>Status</th><th>Score</th><th>Tags</th><th>Last call</th><th></th></tr></thead><tbody>
        ${d.items.map((c) => `<tr class="click" data-id="${c.id}">
          <td>${esc(c.name || '—')}${c.dnc ? ' <span class="tag bad">DNC</span>' : ''}</td><td>${esc(c.phone)}</td><td>${esc(c.company)}</td><td>${esc(c.status)}</td>
          <td>${c.score != null ? `<span class="score">${c.score}</span>` : ''}</td><td>${tags(c.tags)}</td>
          <td class="muted">${when(c.last_call)}</td>
          <td>${c.dnc ? '' : `<button class="btn green small" data-call="${c.id}">Call</button>`}</td></tr>`).join('')}
        </tbody></table>${pager(d.total, contactsState)}`;
      box.querySelectorAll('tr[data-id]').forEach((tr) => {
        tr.onclick = (e) => {
          const c = d.items.find((x) => x.id === Number(tr.dataset.id));
          if (e.target.closest('[data-call]')) { e.stopPropagation(); placeCall(c.phone, c.id, c.name); return; }
          location.hash = '#/contacts/' + c.id;
        };
      });
      bindPager(box, contactsState, load);
    } catch (e) { fail(e); }
  }
  refreshView = load;
  load();
}

function editContact(c, done) {
  c = c || { name: '', phone: dialNumber || '', email: '', company: '', country: '', tags: '', status: 'new', notes: '', dnc: 0 };
  modal(`<h2>${c.id ? 'Edit contact' : 'New contact'}</h2>
    <form id="cform">
      <div class="grid2">
        <label>Name <input name="name" value="${esc(c.name)}"></label>
        <label>Phone (with country code) <input name="phone" value="${esc(c.phone)}" placeholder="+92 300 1234567" required></label>
        <label>Email <input name="email" type="email" value="${esc(c.email)}"></label>
        <label>Company <input name="company" value="${esc(c.company)}"></label>
        <label>Country <input name="country" value="${esc(c.country)}"></label>
        <label>Status <select name="status">${options(CONTACT_STATUSES.map((s) => [s, s]), c.status)}</select></label>
      </div>
      <label>Tags (comma separated) <input name="tags" value="${esc(c.tags)}"></label>
      <label>Notes <textarea name="notes">${esc(c.notes)}</textarea></label>
      <label class="switch"><input type="checkbox" name="dnc" ${c.dnc ? 'checked' : ''}> Do not call (never dialled by campaigns)</label>
      <p class="error" id="cerr"></p>
      <div class="modal-actions">
        ${c.id ? '<button type="button" class="btn red ghost" id="cdel" style="margin-right:auto">Delete</button>' : ''}
        <button type="button" class="btn ghost" id="ccancel">Cancel</button>
        <button class="btn primary">Save</button>
      </div>
    </form>`, (m) => {
    $('#ccancel', m).onclick = closeModal;
    if (c.id) $('#cdel', m).onclick = async () => {
      if (!confirm('Delete this contact? Its call history stays in the call log.')) return;
      await api('/contacts/' + c.id, { method: 'DELETE' }).catch(fail);
      closeModal(); location.hash = '#/contacts';
    };
    $('#cform', m).onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api(c.id ? '/contacts/' + c.id : '/contacts', { method: c.id ? 'PUT' : 'POST', body: formData(e.target) });
        closeModal(); toast('Saved');
        done && done(r);
      } catch (err) { $('#cerr', m).textContent = err.message; }
    };
  });
}

function importCsv(done) {
  modal(`<h2>Import contacts (CSV)</h2>
    <p class="muted">First row = column names. Recognised: <b>name</b> (or first name / last name), <b>phone</b> (or mobile / number),
    email, company, country, tags, notes. Numbers without + get the default country code. Existing numbers are skipped.</p>
    <input type="file" id="csvFile" accept=".csv,text/csv">
    <p class="error" id="ierr"></p><div id="ires"></div>
    <div class="modal-actions"><button class="btn ghost" id="iclose">Close</button><button class="btn primary" id="igo">Import</button></div>`, (m) => {
    $('#iclose', m).onclick = closeModal;
    $('#igo', m).onclick = async () => {
      const f = $('#csvFile', m).files[0];
      if (!f) { $('#ierr', m).textContent = 'Choose a file'; return; }
      try {
        const r = await api('/contacts/import', { method: 'POST', body: { csv: await f.text() } });
        $('#ires', m).innerHTML = `<p><b>${r.added}</b> added, ${r.skipped} skipped.</p>${r.errors.map((x) => `<div class="muted">${esc(x)}</div>`).join('')}`;
        done && done();
      } catch (err) { $('#ierr', m).textContent = err.message; }
    };
  });
}

async function viewContact(el, id) {
  el.innerHTML = '<div class="empty">Loading…</div>';
  let c;
  try { c = await api('/contacts/' + id); } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  let custom = {};
  try { custom = JSON.parse(c.custom || '{}'); } catch { /* */ }
  el.innerHTML = `<div class="toolbar"><a href="#/contacts" class="btn ghost small">${icon('back')} Contacts</a></div>
    <div class="card panel">
      <div class="toolbar" style="margin:0">
        <div style="flex:1"><h2 style="margin:0">${esc(c.name || c.phone)} ${c.dnc ? '<span class="tag bad">Do not call</span>' : ''}</h2>
          <div class="muted">${esc(c.company)}</div></div>
        ${c.score != null ? `<div class="score big" title="AI lead score">${c.score}</div>` : ''}
        ${c.dnc ? '' : `<button class="btn green" id="ccall">Call ${esc(c.phone)}</button>`}
        <button class="btn" id="cedit">Edit</button>
      </div>
      <dl class="kv">
        <dt>Phone</dt><dd>${esc(c.phone)}</dd>
        <dt>Email</dt><dd>${c.email ? `<a href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : '—'}</dd>
        <dt>Country</dt><dd>${esc(c.country || '—')}</dd>
        <dt>Status</dt><dd>${esc(c.status)}</dd>
        <dt>Tags</dt><dd>${tags(c.tags) || '—'}</dd>
        ${Object.entries(custom).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</dd>`).join('')}
        <dt>Added</dt><dd>${when(c.created_at)}</dd>
      </dl>
      ${c.notes ? `<div class="note">${esc(c.notes).replace(/\n/g, '<br>')}</div>` : ''}
    </div>
    <h3>Call history</h3>
    <div class="card table-wrap">${callsTable(c.calls, false)}</div>`;
  if ($('#ccall')) $('#ccall').onclick = () => placeCall(c.phone, c.id, c.name);
  $('#cedit').onclick = () => editContact(c, () => viewContact(el, id));
  bindCallsTable(el, () => viewContact(el, id));
  refreshView = () => { if (location.hash === '#/contacts/' + id) viewContact(el, id); };
}

// --------------------------------------------------------------- calls ----
function callsTable(items, showContact = true) {
  if (!items.length) return '<div class="empty"><b>No calls yet</b>Calls you make or receive show up here.</div>';
  return `<table><thead><tr><th>When</th><th></th>${showContact ? '<th>Contact / number</th>' : '<th>Number</th>'}<th>By</th><th>Status</th><th>Talk</th><th>Outcome</th><th>Summary / notes</th></tr></thead><tbody>
    ${items.map((k) => `<tr class="click" data-call-id="${k.id}">
      <td class="muted">${when(k.started_at)}</td>
      <td title="${k.direction === 'in' ? 'Incoming' : 'Outgoing'}">${k.direction === 'in' ? '↙' : '↗'}</td>
      <td>${showContact && k.contact_name ? `<a href="#/contacts/${k.contact_id}">${esc(k.contact_name)}</a><br><span class="muted">${esc(k.number)}</span>` : esc(k.number)}
        ${k.campaign_name ? `<br><span class="tag">${esc(k.campaign_name)}</span>` : ''}</td>
      <td>${k.ai_agent_name ? `<span class="tag ai">AI ${esc(k.ai_agent_name)}</span>` : esc(k.agent_name || '—')}</td>
      <td class="st-${esc(k.status)}">${esc(STATUS_LABEL[k.status] || k.status)}</td>
      <td>${k.duration ? dur(k.duration) : ''}</td>
      <td>${esc(outcomeLabel(k.disposition))}${k.score != null ? ` <span class="score">${k.score}</span>` : ''}</td>
      <td class="muted ellipsis">${k.has_recording ? '🎙 ' : ''}${esc(k.summary || k.notes)}</td></tr>`).join('')}
    </tbody></table>`;
}

function bindCallsTable(el) {
  el.querySelectorAll('tr[data-call-id]').forEach((tr) => {
    tr.onclick = (e) => { if (!e.target.closest('a')) location.hash = '#/calls/' + tr.dataset.callId; };
  });
}

async function viewCallPage(el, id) {
  el.innerHTML = '<div class="empty">Loading…</div>';
  let k;
  try { k = await api('/calls/' + id); } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const fields = Object.entries(k.ai_fields || {});
  const who = k.contact_name || k.number;
  const by = k.ai_agent_name ? `<span class="tag ai">AI · ${esc(k.ai_agent_name)}</span>` : esc(k.agent_name || '—');
  const roleName = (r) => ({ agent: k.ai_agent_name || k.agent_name || 'Agent', contact: k.contact_name || 'Contact', system: '' }[r] ?? r);
  const sentimentTag = k.sentiment ? `<span class="tag ${k.sentiment === 'positive' ? 'ok' : k.sentiment === 'negative' ? 'bad' : ''}">${esc(k.sentiment)}</span>` : '';
  el.innerHTML = `<div class="toolbar"><a href="#/calls" class="btn ghost small">${icon('back')} Calls</a></div>
    <div class="card panel">
      <div class="call-hero">
        <div style="flex:1;min-width:220px">
          <div class="who">${k.contact_id ? `<a href="#/contacts/${k.contact_id}">${esc(who)}</a>` : esc(who)}</div>
          <div class="muted">${k.direction === 'in' ? 'Incoming' : 'Outgoing'} · ${esc(k.number)} · ${when(k.started_at)}</div>
        </div>
        ${k.score != null ? `<div class="score big" title="AI lead score">${k.score}</div>` : ''}
        <button class="btn green" id="kcall">${icon('calls')} Call back</button>
      </div>
      <div class="stats" style="margin:16px 0 0">
        <div class="card stat"><span>Status</span><b class="st-${esc(k.status)}" style="font-size:18px">${esc(STATUS_LABEL[k.status] || k.status)}</b>${k.cause ? `<small>${esc(k.cause)}</small>` : ''}</div>
        <div class="card stat"><span>Talk time</span><b style="font-size:18px">${dur(k.duration)}</b></div>
        <div class="card stat"><span>Handled by</span><b style="font-size:15px">${by}</b><small>via ${esc(k.provider)}</small></div>
        ${k.campaign_name ? `<div class="card stat"><span>Campaign</span><b style="font-size:15px">${esc(k.campaign_name)}</b></div>` : ''}
      </div>
    </div>
    <div class="detail-grid">
      <div>
        ${k.has_recording ? `<div class="card panel"><h2>Recording</h2><audio controls preload="none" src="/api/calls/${k.id}/recording"></audio></div>` : ''}
        <div class="card panel"><h2>Transcript</h2>
          ${k.transcript.length ? `<div class="chat">${k.transcript.map((t) => `<div class="bubble ${esc(t.role)}">
              ${roleName(t.role) ? `<small>${esc(roleName(t.role))}</small>` : ''}${esc(t.text)}</div>`).join('')}</div>`
            : '<div class="empty" style="padding:20px">No transcript for this call.</div>'}
        </div>
      </div>
      <div>
        <div class="card panel"><div class="card-head"><h2>AI insights</h2>
            ${k.transcript.length || k.has_recording ? `<button class="btn small" id="kai">${icon('spark')} Re-analyse</button>` : ''}</div>
          ${k.analysis === 'pending' ? '<p class="muted">Analysing…</p>' : ''}
          ${(k.analysis || '').startsWith('error') ? `<div class="note warn">Analysis failed: ${esc(k.analysis.slice(7))}</div>` : ''}
          ${k.summary ? `<div class="insight"><span>Summary</span>${esc(k.summary)}</div>` : ''}
          ${k.sentiment || k.score != null ? `<div class="insight"><span>Sentiment &amp; score</span>${sentimentTag} ${k.score != null ? `<span class="score">${k.score}</span>` : ''}</div>` : ''}
          ${k.next_step ? `<div class="insight"><span>Next step</span>${esc(k.next_step)}${k.callback_at ? `<br><b>Call back ${when(k.callback_at)}</b>` : ''}</div>` : ''}
          ${fields.length ? `<div class="insight"><span>What we learned</span><dl class="kv" style="margin:0">${fields.map(([a, b]) =>
            `<dt>${esc(a)}</dt><dd>${esc(typeof b === 'object' ? JSON.stringify(b) : b)}</dd>`).join('')}</dl></div>` : ''}
          ${!k.summary && !fields.length && k.analysis !== 'pending' ? '<p class="muted">No AI insights yet – they appear after a call with a transcript or recording.</p>' : ''}
        </div>
        <div class="card panel"><h2>Outcome</h2>
          <label>Result <select id="kout">${options(OUTCOMES, k.disposition)}</select></label>
          <label>Notes <textarea id="knotes" placeholder="What was agreed?">${esc(k.notes)}</textarea></label>
          <button class="btn primary" id="ksave">Save</button>
        </div>
      </div>
    </div>`;
  $('#kcall').onclick = () => placeCall(k.number, k.contact_id, k.contact_name || '');
  if ($('#kai')) $('#kai').onclick = async () => {
    await api(`/calls/${k.id}/analyze`, { method: 'POST' }).catch(fail);
    toast('Analysis started'); setTimeout(() => viewCallPage(el, id), 4000);
  };
  $('#ksave').onclick = async () => {
    try { await api('/calls/' + k.id, { method: 'PATCH', body: { disposition: $('#kout').value, notes: $('#knotes').value } }); toast('Saved'); }
    catch (err) { fail(err); }
  };
  refreshView = () => {};
}

const callsState = { q: '', direction: '', status: '', mine: false, ai: false, offset: 0 };

function viewCalls(el) {
  el.innerHTML = `${pageHead('Calls', 'Every call – human and AI – with recordings, transcripts and AI insights.')}
    <div class="toolbar">
      <input id="kq" type="search" placeholder="Search number, contact, notes…" value="${esc(callsState.q)}">
      <select id="kdir">${options([['', 'In & out'], ['in', 'Incoming'], ['out', 'Outgoing']], callsState.direction)}</select>
      <select id="kst">${options([['', 'Any status'], ...Object.entries(STATUS_LABEL).filter(([x]) => !['new', 'dialing', 'ringing'].includes(x))], callsState.status)}</select>
      <label class="switch"><input type="checkbox" id="kai" ${callsState.ai ? 'checked' : ''}> AI calls</label>
      ${session.me.user.role === 'admin' ? `<label class="switch"><input type="checkbox" id="kmine" ${callsState.mine ? 'checked' : ''}> Only mine</label>` : ''}
    </div>
    <div class="card table-wrap" id="klist"><div class="empty">Loading…</div></div>`;
  let t;
  $('#kq').oninput = (e) => { clearTimeout(t); t = setTimeout(() => { callsState.q = e.target.value; callsState.offset = 0; load(); }, 250); };
  $('#kdir').onchange = (e) => { callsState.direction = e.target.value; callsState.offset = 0; load(); };
  $('#kst').onchange = (e) => { callsState.status = e.target.value; callsState.offset = 0; load(); };
  $('#kai').onchange = (e) => { callsState.ai = e.target.checked; callsState.offset = 0; load(); };
  if ($('#kmine')) $('#kmine').onchange = (e) => { callsState.mine = e.target.checked; callsState.offset = 0; load(); };
  async function load() {
    try {
      const qs = new URLSearchParams({ q: callsState.q, direction: callsState.direction, status: callsState.status,
        mine: callsState.mine, ai: callsState.ai, limit: 50, offset: callsState.offset });
      const d = await api('/calls?' + qs);
      const box = $('#klist');
      if (!box) return;
      box.innerHTML = callsTable(d.items) + pager(d.total, callsState);
      bindCallsTable(box, load);
      bindPager(box, callsState, load);
    } catch (e) { fail(e); }
  }
  refreshView = load;
  load();
}

boot();
