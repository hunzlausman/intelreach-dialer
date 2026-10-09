// Admin: Twilio number, SIP trunks + numbers, settings, provider integrations, agents.
import { $, api, closeModal, esc, fail, formData, modal, options, session, toast } from './core.js';

export async function viewAdmin(el, ctx) {
  ctx.setRefresh(() => {});
  el.innerHTML = `<div class="tabs">
      <a href="#/admin/twilio" data-tab="twilio">Twilio number</a><a href="#/admin/trunks" data-tab="trunks">SIP trunks</a>
      <a href="#/admin/settings" data-tab="settings">Settings</a>
      <a href="#/admin/integrations" data-tab="integrations">Integrations</a><a href="#/admin/agents" data-tab="agents">Agents</a>
      <span class="muted" style="margin-left:auto;align-self:center;font-size:12px">version ${esc(session.me.version || '?')}</span></div>
    <div id="adminBody"></div>`;
  const tab = ctx.sub || 'twilio';
  el.querySelectorAll('[data-tab]').forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
  const body = $('#adminBody');
  ({ twilio: loadTwilio, trunks: loadTrunks, settings: loadSettings, integrations: loadIntegrations, agents: loadUsers }[tab] || loadTwilio)(body, ctx);
}

// ----------------------------------------------------------------- Twilio ----
async function loadTwilio(p, ctx) {
  p.innerHTML = '<div class="card panel"><div class="muted">Checking Twilio…</div></div>';
  let t;
  try { t = await api('/admin/twilio'); } catch (e) { p.innerHTML = `<div class="card panel error">${esc(e.message)}</div>`; return; }
  const warn = [];
  if (!t.credentials) warn.push('Twilio Account SID / Auth Token are not set yet – Admin → Integrations → Twilio.');
  if (t.error) warn.push(t.error);
  if (t.credentials && t.number && !t.connected && !t.error) warn.push(`Incoming calls are NOT going through the CRM right now (the number's webhook is ${t.currentApp ? 'a TwiML App ' + t.currentApp : t.currentUrl || 'empty'}). If GHL re-saved the number, click Re-connect.`);
  if (t.trunk) warn.push('The number is attached to an Elastic SIP Trunk – remove it from the trunk in Twilio (the trunk is only for outgoing calls).');
  p.innerHTML = `<div class="card panel"><h2>Twilio number</h2>
    ${warn.map((w) => `<div class="note warn" style="margin-bottom:8px">${esc(w)}</div>`).join('')}
    <dl class="kv">
      <dt>Number</dt><dd>${esc(t.number || '—')} ${t.connected ? '<span class="pill ready">incoming → CRM first</span>' : ''}</dd>
      <dt>GHL fallback</dt><dd>${esc(t.ghlUrl || '—')}</dd><dt>CRM webhook</dt><dd>${esc(t.ourUrl)}</dd>
    </dl>
    <div class="toolbar"><input id="twNum" placeholder="+1 555 123 4567" value="${esc(t.number)}">
      <button class="btn primary" id="twConnect">${t.number ? 'Re-connect' : 'Connect'}</button>
      ${t.number ? '<button class="btn" id="twDisconnect">Give incoming back to GHL</button>' : ''}</div>
    <p class="muted">Connect = incoming calls to this number follow Settings → Incoming calls, and it is the caller ID on any
      SIP trunk that has no number of its own (Admin → SIP trunks). GHL's webhook is saved as the fallback. GHL keeps its own calls and SMS.</p></div>`;
  $('#twConnect').onclick = async () => {
    try { const r = await api('/admin/twilio/connect', { method: 'POST', body: { number: $('#twNum').value } }); session.me.callerId = r.number; toast('Connected'); loadTwilio(p, ctx); ctx.renderPhone(); }
    catch (e) { fail(e); }
  };
  if ($('#twDisconnect')) $('#twDisconnect').onclick = async () => {
    if (!confirm('Send incoming calls straight to GHL again? Outgoing CRM calls keep working.')) return;
    try { await api('/admin/twilio/disconnect', { method: 'POST' }); toast('Incoming calls go to GHL again'); loadTwilio(p, ctx); } catch (e) { fail(e); }
  };
}

// ------------------------------------------------------------ SIP trunks ----
const yes = (v) => v === true || v === 1 || v === '1';

const INBOUND_LABEL = { agents: 'ring agents', agents_then_ai: 'ring agents, then AI', ai: 'AI agent answers', reject: 'rejected' };

async function loadTrunks(p) {
  const [d, ag] = await Promise.all([api('/admin/trunks').catch(fail), api('/ai-agents').catch(() => ({ items: [] }))]);
  if (!d) return;
  d.aiAgents = ag.items.filter((a) => a.kind === 'custom');
  const tname = (id) => (d.items.find((t) => t.id === id) || {}).name || '—';
  const def = d.items.find((t) => String(t.id) === String(d.defaultTrunk)) || d.items.find((t) => t.enabled);
  p.innerHTML = `<div class="card panel"><div class="toolbar" style="margin:0 0 8px"><h2 style="margin:0;flex:1">SIP trunks</h2>
      <button class="btn primary" id="tadd">+ Trunk</button></div>
    <p class="muted">Agent calls leave through a SIP trunk from any provider. Add as many as you like – the default is chosen in
      Settings, campaigns can pick another, and a number's own trunk is used when it is the caller ID.
      Changes reach Asterisk within a few seconds.</p>
    ${d.items.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Provider</th><th>SIP server</th><th>Sign-in</th>
      <th>Incoming from</th><th>Status</th><th></th></tr></thead><tbody>
    ${d.items.map((t) => `<tr><td>${esc(t.name)} ${def && def.id === t.id ? '<span class="pill ready">default</span>' : ''}</td>
      <td>${esc((d.vendors[t.vendor] || {}).label || t.vendor)}</td><td>${esc(t.host)}${t.port !== 5060 ? ':' + t.port : ''}</td>
      <td>${t.username ? esc(t.username) + (t.register ? ' (registers)' : '') : '<span class="muted">IP only</span>'}</td>
      <td>${t.vendor === 'twilio' ? 'Twilio IPs' : t.inbound_ips ? esc(t.inbound_ips.split('\n').length + ' IP range(s)') : t.register ? 'registration' : '<span class="muted">—</span>'}</td>
      <td>${t.enabled ? '<span class="st-answered">on</span>' : '<span class="muted">off</span>'}</td>
      <td><button class="btn small" data-tid="${t.id}">Edit</button></td></tr>`).join('')}
    </tbody></table></div>` : '<div class="note warn">No SIP trunk yet – agents cannot call out until you add one.</div>'}</div>
    <div class="card panel"><div class="toolbar" style="margin:0 0 8px"><h2 style="margin:0;flex:1">Numbers</h2>
      <button class="btn primary" id="nadd" ${d.items.length ? '' : 'disabled'}>+ Number</button></div>
    <p class="muted">Numbers you own at any of these providers. A number is the caller ID on calls through its trunk; calls to it
      ring the CRM agents (at the provider, send the number to <b>${esc(d.inboundUri)}</b>).
      ${d.twilioNumber ? `The connected Twilio number ${esc(d.twilioNumber)} is used when a trunk has no number of its own.` : ''}</p>
    ${d.numbers.length ? `<div class="table-wrap"><table><thead><tr><th>Number</th><th>Label</th><th>Trunk</th><th>Incoming calls</th><th></th></tr></thead><tbody>
    ${d.numbers.map((n) => `<tr><td>${esc(n.number)}</td><td>${esc(n.label)}</td><td>${esc(n.trunk_id ? tname(n.trunk_id) : '—')}</td>
      <td>${n.inbound === 'reject' ? '<span class="muted">rejected</span>' : esc(INBOUND_LABEL[n.inbound] || n.inbound)}${
        n.ai_agent_id && n.inbound !== 'agents' && n.inbound !== 'reject' ? ' · ' + esc((d.aiAgents.find((a) => a.id === n.ai_agent_id) || {}).name || 'AI agent') : ''}</td>
      <td><button class="btn small" data-nid="${n.id}">Edit</button></td></tr>`).join('')}
    </tbody></table></div>` : ''}</div>`;
  const again = () => loadTrunks(p);
  $('#tadd').onclick = () => editTrunk(null, d, again);
  $('#nadd').onclick = () => editNumber(null, d, again);
  p.querySelectorAll('[data-tid]').forEach((b) => { b.onclick = () => editTrunk(d.items.find((t) => t.id === Number(b.dataset.tid)), d, again); });
  p.querySelectorAll('[data-nid]').forEach((b) => { b.onclick = () => editNumber(d.numbers.find((n) => n.id === Number(b.dataset.nid)), d, again); });
}

function editTrunk(t, d, done) {
  const n = !t;
  t = t || { name: '', vendor: 'twilio', host: '', port: 5060, username: '', password: '', from_user: '', from_domain: '',
    register: 0, inbound_ips: '', dial_format: 'e164', enabled: 1 };
  modal(`<h2>${n ? 'New SIP trunk' : 'Edit ' + esc(t.name)}</h2><form id="tform">
    <div class="grid2">
      <label>Provider <select name="vendor">${options(Object.entries(d.vendors).map(([k, v]) => [k, v.label]), t.vendor)}</select></label>
      <label>Name <input name="name" value="${esc(t.name)}" placeholder="e.g. Twilio US"></label>
    </div>
    <p class="note" id="thelp"></p>
    <div class="grid2">
      <label>SIP server (termination host) <input name="host" value="${esc(t.host)}" required></label>
      <label>Port <input name="port" type="number" min="1" max="65535" value="${esc(t.port)}"></label>
      <label>Username (empty = IP authentication) <input name="username" value="${esc(t.username)}" autocomplete="off"></label>
      <label>Password <input name="password" type="password" autocomplete="new-password" placeholder="${t.password ? 'saved – leave empty to keep' : ''}"></label>
      <label>Number format the provider wants <select name="dial_format">${options([['e164', '+15551234567 (E.164)'], ['digits', '15551234567 (no +)']], t.dial_format)}</select></label>
    </div>
    <label class="switch"><input type="checkbox" name="register" ${yes(t.register) ? 'checked' : ''}> Register with the provider (it sends incoming calls to the registration)</label>
    <label data-ips>Provider IPs that send incoming calls – one per line (the firewall opens SIP to them)
      <textarea name="inbound_ips" rows="3" placeholder="192.0.2.10&#10;198.51.100.0/24">${esc(t.inbound_ips)}</textarea></label>
    <details><summary class="muted">Advanced</summary><div class="grid2">
      <label>From user (some providers want the username or number) <input name="from_user" value="${esc(t.from_user)}"></label>
      <label>From domain (empty = SIP server) <input name="from_domain" value="${esc(t.from_domain)}"></label>
    </div></details>
    <label class="switch"><input type="checkbox" name="enabled" ${yes(t.enabled) ? 'checked' : ''}> Enabled</label>
    <p class="error" id="terr"></p>
    <div class="modal-actions">
      ${n ? '' : '<button type="button" class="btn red ghost" id="tdel" style="margin-right:auto">Delete</button>'}
      <button type="button" class="btn ghost" id="tcancel">Cancel</button><button class="btn primary">Save</button></div>
  </form>`, (m) => {
    const f = $('#tform', m);
    const el = (k) => f.elements.namedItem(k);
    const vendor = el('vendor');
    const sync = (prefill) => {
      const v = d.vendors[vendor.value] || {};
      $('#thelp', m).textContent = v.help || '';
      el('host').placeholder = v.host || 'sip.provider.com';
      $('[data-ips]', m).hidden = vendor.value === 'twilio';
      if (prefill) {
        el('host').value = v.host && !v.host.includes('<') ? v.host : '';
        el('register').checked = !!v.register;
        el('dial_format').value = v.dial_format || 'e164';
        el('inbound_ips').value = v.inbound_ips || '';
        if (!el('name').value || Object.values(d.vendors).some((x) => x.label === el('name').value)) el('name').value = v.label || '';
      }
    };
    vendor.onchange = () => sync(n);
    sync(n && !t.host);
    $('#tcancel', m).onclick = closeModal;
    if (!n) $('#tdel', m).onclick = async () => {
      if (!confirm(`Delete the trunk ${t.name}? Its numbers stay, without a trunk.`)) return;
      try { await api('/admin/trunks/' + t.id, { method: 'DELETE' }); closeModal(); toast('Trunk deleted'); done(); } catch (e) { fail(e); }
    };
    f.onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api(n ? '/admin/trunks' : '/admin/trunks/' + t.id, { method: n ? 'POST' : 'PUT', body: formData(f) });
        closeModal(); toast(r.warning || 'Trunk saved', !!r.warning); done();
      } catch (err) { $('#terr', m).textContent = err.message; }
    };
  });
}

function editNumber(num, d, done) {
  const n = !num;
  num = num || { number: '', label: '', trunk_id: (d.items.find((t) => t.enabled) || {}).id, inbound: 'agents' };
  modal(`<h2>${n ? 'Add a number' : 'Edit ' + esc(num.number)}</h2><form id="nform">
    <label>Number <input name="number" value="${esc(num.number)}" placeholder="+15551234567" required></label>
    <label>Label <input name="label" value="${esc(num.label)}" placeholder="e.g. US sales line"></label>
    <label>Provider / trunk <select name="trunk_id">${options([['', '— none —'], ...d.items.map((t) => [t.id, t.name])], num.trunk_id)}</select></label>
    <label>Incoming calls to this number <select name="inbound">${options([['agents', 'Ring the CRM agents'],
      ['agents_then_ai', 'Ring the CRM agents, then the AI agent'], ['ai', 'AI agent answers'], ['reject', 'Reject']], num.inbound)}</select></label>
    <label data-ai>AI agent (Custom agents only) <select name="ai_agent_id">${options([['', '— choose —'],
      ...d.aiAgents.map((a) => [a.id, a.name])], num.ai_agent_id)}</select></label>
    <p class="error" id="nerr"></p>
    <div class="modal-actions">
      ${n ? '' : '<button type="button" class="btn red ghost" id="ndel" style="margin-right:auto">Delete</button>'}
      <button type="button" class="btn ghost" id="ncancel">Cancel</button><button class="btn primary">Save</button></div>
  </form>`, (m) => {
    const inb = m.querySelector('[name=inbound]');
    const syncAi = () => { $('[data-ai]', m).hidden = !['ai', 'agents_then_ai'].includes(inb.value); };
    inb.onchange = syncAi; syncAi();
    $('#ncancel', m).onclick = closeModal;
    if (!n) $('#ndel', m).onclick = async () => {
      if (!confirm(`Remove ${num.number} from the CRM? (It stays with the provider.)`)) return;
      try { await api('/admin/numbers/' + num.id, { method: 'DELETE' }); closeModal(); done(); } catch (e) { fail(e); }
    };
    $('#nform', m).onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api(n ? '/admin/numbers' : '/admin/numbers/' + num.id, { method: n ? 'POST' : 'PUT', body: formData(e.target) });
        closeModal(); toast('Saved'); done();
      } catch (err) { $('#nerr', m).textContent = err.message; }
    };
  });
}

// --------------------------------------------------------------- Settings ----
async function loadSettings(p) {
  const [s, agents] = await Promise.all([api('/admin/settings').catch(fail), api('/ai-agents').catch(() => ({ items: [] }))]);
  if (!s) return;
  const agentOpts = [['', '— none —'], ...agents.items.map((a) => [a.id, `${a.name} (${a.kind})`])];
  const chk = (name, label) => `<label class="switch"><input type="checkbox" name="${name}" ${s[name] === '1' ? 'checked' : ''}> ${label}</label>`;
  p.innerHTML = `<form id="sform"><div class="card panel"><h2>Calls</h2><div class="grid2">
      <label>Company name <input name="company_name" value="${esc(s.company_name)}"></label>
      <label>Default country code (numbers typed without +) <input name="default_country" value="${esc(s.default_country)}" placeholder="+1"></label>
      <label>Allowed countries (prefixes, * = all) <input name="allowed_prefixes" value="${esc(s.allowed_prefixes)}" placeholder="+1,+44,+92,+971"></label>
      <label>Blocked prefixes <input name="blocked_prefixes" value="${esc(s.blocked_prefixes)}" placeholder="+882,+883"></label>
      <label>Longest call (minutes) <input name="max_call_minutes" type="number" min="1" value="${esc(s.max_call_minutes)}"></label>
      <label>Agent calls leave through <select name="default_trunk">${options([['', 'First enabled trunk'],
        ...s._trunks.map((t) => [t.id, `${t.name} (${t.vendor})`])], s.default_trunk)}</select></label>
    </div>${s._trunks.length ? '' : '<p class="muted">No SIP trunk yet – add one in Admin → SIP trunks.</p>'}</div>
    <div class="card panel"><h2>Incoming calls to the Twilio number</h2><div class="grid2">
      <label>Route <select name="inbound_mode">${options([['crm_then_ghl', 'Ring CRM agents, then GHL'], ['crm_then_ai', 'Ring CRM agents, then AI agent'],
        ['ai_only', 'AI agent answers'], ['crm_only', 'CRM agents only'], ['ghl_only', 'GHL only']], s.inbound_mode)}</select></label>
      <label>Ring CRM agents for (seconds) <input name="ring_timeout" type="number" min="5" max="120" value="${esc(s.ring_timeout)}"></label>
      <label>AI agent for incoming calls <select name="inbound_ai_agent">${options(agentOpts, s.inbound_ai_agent)}</select></label>
      <label>AI agent for calls to Telnyx numbers <select name="telnyx_inbound_agent">${options(agentOpts, s.telnyx_inbound_agent)}</select></label>
    </div></div>
    <div class="card panel"><h2>Recording &amp; AI on agent calls</h2>
      <div class="grid2" style="margin-bottom:8px">
        <label>Speech-to-text (captions, recording transcripts, AI agents' default) <select name="stt_provider">${options([
          ['assemblyai', 'AssemblyAI'], ['deepgram', 'Deepgram']], s.stt_provider)}</select></label>
      </div>
      ${chk('record_calls', 'Record agent calls (tell callers / follow your local consent laws)')}
      ${chk('transcribe_recordings', 'Transcribe recordings')}
      ${chk('live_captions', 'Live captions + AI tips for agents during calls')}
      ${chk('analyze_calls', 'AI summary, outcome, sentiment and lead score after every call')}
      <div class="grid2" style="margin-top:8px">
        <label>LLM for summaries and tips <select name="analysis_llm">${options([['assemblyai', 'AssemblyAI LLM Gateway'], ['anthropic', 'Anthropic Claude'], ['openai', 'OpenAI'],
          ['gemini', 'Google Gemini'], ['custom_llm', 'OpenAI-compatible']], s.analysis_llm)}</select></label>
        <label>Model <input name="analysis_model" value="${esc(s.analysis_model)}" placeholder="claude-opus-5-5"></label>
      </div></div>
    <button class="btn primary">Save settings</button></form>`;
  $('#sform').onsubmit = async (e) => {
    e.preventDefault();
    const f = formData(e.target);
    ['record_calls', 'transcribe_recordings', 'live_captions', 'analyze_calls'].forEach((k) => { f[k] = f[k] ? '1' : '0'; });
    try { await api('/admin/settings', { method: 'PUT', body: f }); toast('Settings saved'); }
    catch (err) { fail(err); }
  };
}

// ----------------------------------------------------------- Integrations ----
const FIELD_LABEL = { model: 'Model (empty = nova-3)', language: 'Language (empty = automatic / multilingual, or e.g. en, es)', account_sid: 'Account SID', auth_token: 'Auth Token', api_key: 'API key', base_url: 'Base URL', webhook_secret: 'Post-call webhook secret',
  connection_id: 'Call Control App ID (connection_id)', public_key: 'Webhook public key', from_number: 'Default caller ID (Telnyx number)' };
const HELP = {
  twilio: 'Console → Account → API keys & tokens. Used for the Twilio number, webhook signatures and Twilio AI calls (SIP trunks: Admin → SIP trunks).',
  anthropic: 'Claude for AI agents, summaries and tips. Default model claude-opus-5-5.',
  openai: 'Leave Base URL empty for api.openai.com.',
  gemini: 'Uses Google\'s OpenAI-compatible endpoint.',
  custom_llm: 'Any OpenAI-compatible API: Groq (https://api.groq.com/openai/v1), DeepSeek, OpenRouter, Together, a local Ollama/vLLM…',
  assemblyai: 'Speech-to-text (live captions, AI-call listening, recording transcripts) – and the same key runs the LLM Gateway (choose "AssemblyAI LLM Gateway" as an agent\'s LLM). Model to test with = a Gateway model.',
  deepgram: 'Speech-to-text (choose it in Settings, or per AI agent). The key needs the Member role or higher so the CRM can issue live-caption tokens.',
  elevenlabs: 'Voices for the custom pipeline and voicemail drops, and ElevenLabs agents. Webhook → Settings → Post-call webhook.',
  telnyx: 'Call Control App: set its webhook URL to the one below. Public key: Portal → Keys & Credentials.',
};

async function loadIntegrations(p) {
  const d = await api('/admin/integrations').catch(fail);
  if (!d) return;
  p.innerHTML = `<div class="note" style="margin-bottom:12px">Webhook URLs to paste into the providers:<br>
      Telnyx Call Control App → <b>${esc(d.webhooks.telnyx)}</b><br>ElevenLabs post-call webhook → <b>${esc(d.webhooks.elevenlabs)}</b><br>
      Keys are stored encrypted and never shown again – leave a key field empty to keep the saved one.</div>
    ${d.items.map((it) => `<form class="card panel" data-prov="${it.provider}">
      <div class="toolbar" style="margin:0 0 6px"><h2 style="margin:0;flex:1">${esc(it.label)}</h2>
        <span class="pill ${it.configured ? 'ready' : ''}">${it.configured ? 'configured' : 'not set'}</span></div>
      <p class="muted">${esc(HELP[it.provider] || '')}</p>
      <div class="grid2">${it.fields.map((f) => `<label>${esc(FIELD_LABEL[f] || f)}
        <input name="${f}" ${it.secret.includes(f) ? `type="password" placeholder="${esc(it[f] || '')}" autocomplete="new-password"` : `value="${esc(it[f] || '')}"`}></label>`).join('')}</div>
      ${d.defaultModels[it.provider] !== undefined ? `<label>Model to test with <input name="_model" value="${esc(d.defaultModels[it.provider])}"></label>` : ''}
      <div class="toolbar" style="margin:0"><button class="btn primary">Save</button><button type="button" class="btn" data-test>Test</button>
        <span class="muted" data-result></span></div>
    </form>`).join('')}`;
  p.querySelectorAll('form[data-prov]').forEach((form) => {
    const prov = form.dataset.prov;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const { _model, ...vals } = formData(form);
      try { await api('/admin/integrations/' + prov, { method: 'PUT', body: vals }); toast('Saved'); loadIntegrations(p); }
      catch (err) { fail(err); }
    };
    form.querySelector('[data-test]').onclick = async () => {
      const out = form.querySelector('[data-result]');
      out.textContent = 'Testing…';
      const model = form.querySelector('[name=_model]');
      try {
        const r = await api(`/admin/integrations/${prov}/test`, { method: 'POST', body: { model: model ? model.value : '' } });
        out.textContent = (r.ok ? '✓ ' : '✗ ') + r.message;
        out.className = r.ok ? 'st-answered' : 'error';
      } catch (e) { out.textContent = '✗ ' + e.message; out.className = 'error'; }
    };
  });
}

// ----------------------------------------------------------------- Agents ----
async function loadUsers(p) {
  const d = await api('/admin/users').catch(fail);
  if (!d) return;
  p.innerHTML = `<div class="card panel"><div class="toolbar" style="margin:0 0 8px"><h2 style="margin:0;flex:1">Agents</h2>
      <span class="muted">${d.freeLines} free phone lines</span><button class="btn primary" id="uadd">+ Agent</button></div>
    <div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Line</th><th>Status</th><th></th></tr></thead><tbody>
    ${d.items.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td><td>${esc(u.role)}</td><td>${esc(u.sip_ext || '—')}</td>
      <td>${!u.active ? '<span class="muted">disabled</span>' : u.online ? (u.available ? '<span class="st-answered">online</span>' : 'online, not taking calls') : '<span class="muted">offline</span>'}</td>
      <td><button class="btn small" data-uid="${u.id}">Edit</button></td></tr>`).join('')}
    </tbody></table></div></div>`;
  $('#uadd').onclick = () => editUser(null, () => loadUsers(p));
  p.querySelectorAll('[data-uid]').forEach((b) => { b.onclick = () => editUser(d.items.find((u) => u.id === Number(b.dataset.uid)), () => loadUsers(p)); });
}

function editUser(u, done) {
  const n = !u;
  u = u || { name: '', email: '', role: 'agent', active: 1 };
  modal(`<h2>${n ? 'New agent' : 'Edit ' + esc(u.name)}</h2><form id="uform">
      <label>Name <input name="name" value="${esc(u.name)}" required></label>
      <label>Email (sign-in) <input name="email" type="email" value="${esc(u.email)}" required></label>
      <label>Role <select name="role">${options([['agent', 'Agent'], ['admin', 'Admin']], u.role)}</select></label>
      <label>${n ? 'Password' : 'New password (leave empty to keep)'} <input name="password" type="password" minlength="8" ${n ? 'required' : ''} autocomplete="new-password"></label>
      <label class="switch"><input type="checkbox" name="active" ${u.active ? 'checked' : ''}> Active</label>
      <p class="error" id="uerr"></p>
      <div class="modal-actions"><button type="button" class="btn ghost" id="ucancel">Cancel</button><button class="btn primary">Save</button></div>
    </form>`, (m) => {
    $('#ucancel', m).onclick = closeModal;
    $('#uform', m).onsubmit = async (e) => {
      e.preventDefault();
      const body = formData(e.target);
      try {
        const r = await api(n ? '/admin/users' : '/admin/users/' + u.id, { method: n ? 'POST' : 'PUT', body });
        closeModal(); toast(r.warning || 'Saved', !!r.warning); done();
      } catch (err) { $('#uerr', m).textContent = err.message; }
    };
  });
}
