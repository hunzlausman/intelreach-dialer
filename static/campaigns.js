// Campaigns: power dialer (agents), AI agent calling, voicemail drop.
import { $, api, bindPager, closeModal, CONTACT_STATUSES, dur, esc, fail, formData, modal, options, pageHead, pager, session, toast, when } from './core.js';

const KIND_LABEL = { power: 'Power dialer', ai: 'AI agent calls', voicemail: 'Voicemail drop' };
const STATUS_PILL = { running: 'ready', paused: 'connecting', draft: '', completed: '' };
const DAYS = [['1', 'Mon'], ['2', 'Tue'], ['3', 'Wed'], ['4', 'Thu'], ['5', 'Fri'], ['6', 'Sat'], ['7', 'Sun']];

function statsLine(s) {
  const l = s.leads || {};
  return `${s.total} leads · ${l.pending || 0} waiting · ${l.retry || 0} retry · ${l.done || 0} done · ${l.failed || 0} failed`
    + ` · ${s.calls} calls · ${s.answered} answered${s.voicemails ? ` · ${s.voicemails} voicemails` : ''}${s.live ? ` · <b>${s.live} live</b>` : ''}`;
}

export async function viewCampaigns(el, ctx) {
  const admin = session.me.user.role === 'admin';
  el.innerHTML = `${pageHead('Campaigns', 'Power dialing for your team, AI calling and voicemail drops.',
      admin ? '<button class="btn primary" id="newCamp">+ Campaign</button>' : '')}
    <div id="clist"><div class="empty">Loading…</div></div>`;
  if (admin) $('#newCamp').onclick = () => editCampaign(null, (c) => { location.hash = '#/campaigns/' + c.id; });
  const load = async () => {
    const d = await api('/campaigns').catch(fail);
    const box = $('#clist');
    if (!d || !box) return;
    if (!d.items.length) { box.innerHTML = '<div class="card empty"><b>No campaigns yet</b>Create one to power-dial a lead list with your team or let an AI agent call it.</div>'; return; }
    box.innerHTML = d.items.map((c) => `<div class="card panel click-card" data-id="${c.id}">
        <div class="toolbar" style="margin:0">
          <div style="flex:1"><b>${esc(c.name)}</b> <span class="tag">${KIND_LABEL[c.kind]}</span>
            <span class="pill ${STATUS_PILL[c.status]}">${esc(c.status)}</span></div>
          ${c.kind === 'power' && c.status === 'running' ? `<button class="btn green small" data-dial="${c.id}">Start dialing</button>` : ''}
        </div>
        <div class="muted" style="margin-top:6px">${statsLine(c.stats)}</div>
        ${c.config.error ? `<div class="note warn" style="margin-top:8px">${esc(c.config.error)}</div>` : ''}
      </div>`).join('');
    box.querySelectorAll('[data-id]').forEach((card) => {
      card.onclick = (e) => {
        const dial = e.target.closest('[data-dial]');
        const c = d.items.find((x) => x.id === Number(card.dataset.id));
        if (dial) { e.stopPropagation(); ctx.startDialing(c); return; }
        location.hash = '#/campaigns/' + c.id;
      };
    });
  };
  ctx.setRefresh(load);
  load();
}

export async function viewCampaign(el, id, ctx) {
  const admin = session.me.user.role === 'admin';
  const st = { status: '', offset: 0 };
  let c;
  const load = async () => {
    try { c = await api('/campaigns/' + id); } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const cfg = c.config;
    el.innerHTML = `<div class="toolbar"><a href="#/campaigns" class="btn ghost">‹ Campaigns</a></div>
      <div class="card panel">
        <div class="toolbar" style="margin:0">
          <div style="flex:1"><h2 style="margin:0">${esc(c.name)}</h2>
            <span class="tag">${KIND_LABEL[c.kind]}</span> <span class="pill ${STATUS_PILL[c.status]}">${esc(c.status)}</span></div>
          ${c.kind === 'power' && c.status === 'running' ? '<button class="btn green" id="dialBtn">Start dialing</button>' : ''}
          ${admin ? `${c.status === 'running' ? '<button class="btn" id="pauseBtn">Pause</button>' : '<button class="btn primary" id="runBtn">Start campaign</button>'}
            <button class="btn" id="editBtn">Edit</button>` : ''}
        </div>
        ${cfg.error ? `<div class="note warn" style="margin-top:8px">Paused: ${esc(cfg.error)}</div>` : ''}
        <div class="stats" style="margin:12px 0 0">${[
          [c.stats.total, 'Leads'], [(c.stats.leads.pending || 0) + (c.stats.leads.retry || 0), 'To call'],
          [c.stats.leads.done || 0, 'Done'], [c.stats.answered, 'Answered'], [c.stats.positive, 'Interested / sale'],
          [dur(c.stats.seconds), 'Talk time'], [c.stats.live, 'Live now'],
        ].map(([v, l]) => `<div class="card stat"><b>${esc(v)}</b><span>${l}</span></div>`).join('')}</div>
        <dl class="kv">
          <dt>Calling hours</dt><dd>${esc(cfg.window_start || '09:00')}–${esc(cfg.window_end || '18:00')} (each lead's local time) ·
            ${DAYS.filter(([d]) => String(cfg.days || '1,2,3,4,5').split(',').includes(d)).map((x) => x[1]).join(' ')}</dd>
          <dt>Attempts</dt><dd>${esc(cfg.max_attempts || 3)} per lead, ${esc(cfg.retry_minutes || 60)} min apart</dd>
          ${c.kind !== 'power' ? `<dt>Parallel calls</dt><dd>${esc(cfg.concurrency || 1)}</dd>` : ''}
          ${cfg.goal ? `<dt>Goal</dt><dd>${esc(cfg.goal)}</dd>` : ''}
        </dl>
      </div>
      <div class="toolbar"><h3 style="margin:0;flex:1">Leads</h3>
        <select id="lst">${options([['', 'All'], ['pending', 'Waiting'], ['calling', 'Calling'], ['retry', 'Retry'],
          ['done', 'Done'], ['failed', 'Failed'], ['dnc', 'Do not call']], st.status)}</select>
        ${admin ? '<button class="btn" id="resetBtn">Retry failed</button><button class="btn primary" id="addBtn">+ Add leads</button>' : ''}
      </div>
      <div class="card table-wrap" id="leads"><div class="empty">Loading…</div></div>`;
    if ($('#dialBtn')) $('#dialBtn').onclick = () => ctx.startDialing(c);
    if ($('#runBtn')) $('#runBtn').onclick = () => setStatus('running');
    if ($('#pauseBtn')) $('#pauseBtn').onclick = () => setStatus('paused');
    if ($('#editBtn')) $('#editBtn').onclick = () => editCampaign(c, load);
    if ($('#addBtn')) $('#addBtn').onclick = () => addLeads(c, load);
    if ($('#resetBtn')) $('#resetBtn').onclick = async () => {
      const r = await api(`/campaigns/${id}/leads/reset`, { method: 'POST' }).catch(fail);
      if (r) { toast(`${r.reset} leads will be tried again`); load(); }
    };
    $('#lst').onchange = (e) => { st.status = e.target.value; st.offset = 0; loadLeads(); };
    loadLeads();
  };
  const setStatus = async (status) => {
    try { await api(`/campaigns/${id}/status`, { method: 'POST', body: { status } }); toast(status === 'running' ? 'Campaign started' : 'Paused'); load(); }
    catch (e) { fail(e); }
  };
  const loadLeads = async () => {
    const d = await api(`/campaigns/${id}/leads?` + new URLSearchParams({ status: st.status, offset: st.offset })).catch(fail);
    const box = $('#leads');
    if (!d || !box) return;
    if (!d.items.length) { box.innerHTML = '<div class="empty">No leads</div>'; return; }
    box.innerHTML = `<table><thead><tr><th>Contact</th><th>Phone</th><th>Status</th><th>Attempts</th><th>Next try</th><th>Result</th><th></th></tr></thead><tbody>
      ${d.items.map((l) => `<tr><td><a href="#/contacts/${l.contact_id}">${esc(l.name || '—')}</a><br><span class="muted">${esc(l.company)}</span></td>
        <td>${esc(l.phone)}<br><span class="muted">${esc(l.tz)}</span></td><td>${esc(l.status)}${l.agent_name ? ` <span class="muted">(${esc(l.agent_name)})</span>` : ''}</td>
        <td>${l.attempts}</td><td class="muted">${l.status === 'retry' ? when(l.next_at) : ''}</td><td>${esc(l.result)}</td>
        <td>${admin && l.status !== 'calling' ? `<button class="btn small ghost" data-del="${l.id}">Remove</button>` : ''}</td></tr>`).join('')}
      </tbody></table>${pager(d.total, st)}`;
    bindPager(box, st, loadLeads);
    box.querySelectorAll('[data-del]').forEach((b) => {
      b.onclick = async () => { await api(`/campaigns/${id}/leads/${b.dataset.del}`, { method: 'DELETE' }).catch(fail); loadLeads(); };
    });
  };
  ctx.setRefresh(load);
  load();
}

async function editCampaign(c, done) {
  const isNew = !c;
  c = c || { name: '', kind: 'power', config: { days: '1,2,3,4,5', window_start: '09:00', window_end: '18:00', max_attempts: 3,
    retry_minutes: 60, concurrency: 2, preview_seconds: 5, vm_tts: 'telnyx', on_human: 'play', trunk: '' } };
  const cfg = c.config;
  const agents = (await api('/ai-agents').catch(() => ({ items: [] }))).items;
  const settings = await api('/admin/settings').catch(() => ({}));
  const days = String(cfg.days || '').split(',');
  modal(`<h2>${isNew ? 'New campaign' : 'Edit ' + esc(c.name)}</h2><form id="cform">
    <div class="grid2">
      <label>Name <input name="name" value="${esc(c.name)}" required></label>
      <label>Type <select name="kind" ${isNew ? '' : 'disabled'}>${options(Object.entries(KIND_LABEL), c.kind)}</select></label>
    </div>
    <label>Goal (used by the AI summary and tips) <input name="goal" value="${esc(cfg.goal)}" placeholder="Book a product demo"></label>
    <div data-kind="power"><label>Call script for agents – {{first_name}}, {{company}}, {{agent}} are filled in
      <textarea name="script" rows="4">${esc(cfg.script)}</textarea></label>
      <div class="grid2">
        <label>Seconds to preview a lead before dialing (0 = dial at once) <input name="preview_seconds" type="number" min="0" value="${esc(cfg.preview_seconds ?? 5)}"></label>
        <label>Agent calls leave through <select name="trunk">${options([['', 'Default (Settings) – or the caller ID number\'s trunk'],
          ...(settings._trunks || []).map((t) => [t.id, `${t.name} (${t.vendor})`]),
          ...(['twilio', 'telnyx'].includes(cfg.trunk) ? [[cfg.trunk, `First ${cfg.trunk} trunk`]] : [])], cfg.trunk)}</select></label>
        <label>Caller ID (empty = the trunk's number) <input name="caller_id" list="cNumbers" value="${esc(cfg.caller_id)}" placeholder="+15551234567">
          <datalist id="cNumbers">${(settings._numbers || []).map((x) => `<option value="${esc(x.number)}">${esc(x.label)}</option>`).join('')}</datalist></label>
      </div></div>
    <div data-kind="ai voicemail"><label>AI agent (makes the calls – or, for voicemail drops, takes over when a person answers)
      <select name="ai_agent_id">${options([['', '— choose —'], ...agents.map((a) => [a.id, `${a.name} (${a.kind})`])], cfg.ai_agent_id)}</select></label></div>
    <div data-kind="ai" class="grid2">
      <label>Calls go out through (Custom agents) <select name="ai_carrier">${options([['sip', 'Your SIP trunk (Admin → SIP trunks)'],
        ['agent', "The AI agent's own Phone carrier"], ['telnyx', 'Telnyx Call Control API'], ['twilio', 'Twilio API']], cfg.ai_carrier || 'sip')}</select></label>
      <label>Caller ID (empty = the agent's / trunk's number) <input name="from_number" list="cNumbers2" value="${esc(cfg.from_number)}" placeholder="+15551234567">
        <datalist id="cNumbers2">${(settings._numbers || []).map((x) => `<option value="${esc(x.number)}">${esc(x.label)}</option>`).join('')}</datalist></label>
    </div>
    <div data-kind="voicemail">
      <label>Voicemail message <textarea name="vm_text" rows="3">${esc(cfg.vm_text)}</textarea></label>
      <div class="grid2">
        <label>Voice <select name="vm_tts">${options([['telnyx', 'Telnyx text-to-speech'], ['elevenlabs', 'ElevenLabs voice (recorded once)']], cfg.vm_tts)}</select></label>
        <label>Voice ID (ElevenLabs voice or Telnyx voice, e.g. female) <input name="vm_voice_id" value="${esc(cfg.vm_voice_id)}"></label>
        <label>When a person answers <select name="on_human">${options([['play', 'Play the message too'], ['transfer', 'Transfer to a number'],
          ['ai', 'Hand over to an AI agent'], ['hangup', 'Hang up']], cfg.on_human)}</select></label>
        <label>Transfer to (+number) <input name="transfer_to" value="${esc(cfg.transfer_to)}" placeholder="+15551234567"></label>
        <label>Caller ID (Telnyx number) <input name="vm_from_number" value="${esc(cfg.from_number)}" placeholder="+15550002222"></label>
      </div>
    </div>
    <div data-kind="ai voicemail"><label>Calls at the same time <input name="concurrency" type="number" min="1" max="50" value="${esc(cfg.concurrency || 1)}"></label></div>
    <h3>Calling rules</h3>
    <div class="grid2">
      <label>From (lead's local time) <input name="window_start" value="${esc(cfg.window_start || '09:00')}" placeholder="09:00"></label>
      <label>Until <input name="window_end" value="${esc(cfg.window_end || '18:00')}" placeholder="18:00"></label>
      <label>Attempts per lead <input name="max_attempts" type="number" min="1" value="${esc(cfg.max_attempts || 3)}"></label>
      <label>Minutes between attempts <input name="retry_minutes" type="number" min="1" value="${esc(cfg.retry_minutes || 60)}"></label>
      <label>Time zone if unknown from the number <input name="timezone" value="${esc(cfg.timezone)}" placeholder="America/New_York"></label>
    </div>
    <div class="days">${DAYS.map(([d, l]) => `<label class="switch"><input type="checkbox" name="day${d}" ${days.includes(d) ? 'checked' : ''}> ${l}</label>`).join('')}</div>
    <p class="error" id="cerr"></p>
    <div class="modal-actions">
      ${!isNew ? '<button type="button" class="btn red ghost" id="cdel" style="margin-right:auto">Delete</button>' : ''}
      <button type="button" class="btn ghost" id="ccancel">Cancel</button><button class="btn primary">Save</button></div>
  </form>`, (m) => {
    const kindSel = m.querySelector('[name=kind]');
    const sync = () => m.querySelectorAll('[data-kind]').forEach((x) => { x.hidden = !x.dataset.kind.split(' ').includes(kindSel.value); });
    kindSel.onchange = sync; sync();
    $('#ccancel', m).onclick = closeModal;
    if (!isNew) $('#cdel', m).onclick = async () => {
      if (!confirm('Delete this campaign and its lead list? Call history stays.')) return;
      try { await api('/campaigns/' + c.id, { method: 'DELETE' }); closeModal(); location.hash = '#/campaigns'; } catch (e) { fail(e); }
    };
    $('#cform', m).onsubmit = async (e) => {
      e.preventDefault();
      const f = formData(e.target);
      const config = {};
      if (f.kind === 'voicemail' || (!isNew && c.kind === 'voicemail')) f.from_number = f.vm_from_number;
      ['goal', 'script', 'preview_seconds', 'trunk', 'caller_id', 'ai_carrier', 'ai_agent_id', 'vm_text', 'vm_tts', 'vm_voice_id', 'on_human',
        'transfer_to', 'from_number', 'concurrency', 'window_start', 'window_end', 'max_attempts', 'retry_minutes', 'timezone']
        .forEach((k) => { if (f[k] !== undefined && f[k] !== '') config[k] = f[k]; });
      config.days = DAYS.filter(([d]) => f['day' + d]).map(([d]) => d).join(',');
      try {
        const r = await api(isNew ? '/campaigns' : '/campaigns/' + c.id, { method: isNew ? 'POST' : 'PUT',
          body: { name: f.name, kind: isNew ? f.kind : c.kind, config } });
        closeModal(); toast('Saved'); done && done(r);
      } catch (err) { $('#cerr', m).textContent = err.message; }
    };
  }, true);
}

function addLeads(c, done) {
  modal(`<h2>Add leads to ${esc(c.name)}</h2>
    <p class="muted">Contacts on the do-not-call list are skipped. Each lead's time zone comes from its phone number.</p>
    <h3>From your contacts</h3>
    <div class="grid2">
      <label>Tag <input id="ltag" placeholder="e.g. webinar"></label>
      <label>Status <select id="lstatus"><option value="">Any</option>${options(CONTACT_STATUSES.map((s) => [s, s]), '')}</select></label>
    </div>
    <label>Search <input id="lq" placeholder="name, company…"></label>
    <button class="btn primary" id="lfilter">Add matching contacts</button>
    <h3>Or upload a CSV</h3>
    <p class="muted">Same columns as the contacts import; new numbers become contacts too.</p>
    <input type="file" id="lcsv" accept=".csv,text/csv"> <button class="btn" id="lupload" style="margin-top:8px">Upload & add</button>
    <p class="error" id="lerr"></p>
    <div class="modal-actions"><button class="btn ghost" id="lclose">Close</button></div>`, (m) => {
    $('#lclose', m).onclick = closeModal;
    const go = async (body) => {
      try {
        const r = await api(`/campaigns/${c.id}/leads`, { method: 'POST', body });
        toast(`${r.added} leads added`); closeModal(); done();
      } catch (e) { $('#lerr', m).textContent = e.message; }
    };
    $('#lfilter', m).onclick = () => go({ all_matching: true, tag: $('#ltag', m).value, status: $('#lstatus', m).value, q: $('#lq', m).value });
    $('#lupload', m).onclick = async () => {
      const f = $('#lcsv', m).files[0];
      if (!f) { $('#lerr', m).textContent = 'Choose a file'; return; }
      go({ csv: await f.text() });
    };
  });
}
