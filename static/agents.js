// AI agent studio: agent cards with readiness, template-based builder, voice preview, test call.
import { $, api, closeModal, esc, fail, icon, modal, options, pageHead, toast } from './core.js';

const ENGINES = [['custom', 'Built-in (recommended)'], ['elevenlabs', 'ElevenLabs hosted agent'],
  ['telnyx', 'Telnyx AI Assistant']];
const LLMS = [['gemini', 'Google Gemini'], ['assemblyai', 'AssemblyAI LLM Gateway'], ['openai', 'OpenAI'],
  ['anthropic', 'Anthropic Claude'], ['custom_llm', 'OpenAI-compatible']];
const MODELS = {
  gemini: ['gemini-2.5-flash', 'gemini-2.5-flash-lite'],
  assemblyai: ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'claude-haiku-4-5-20251001', 'gpt-5-mini', 'gpt-5-nano'],
  openai: ['gpt-5-mini', 'gpt-5-nano', 'gpt-4.1-mini'],
  anthropic: ['claude-haiku-5-5', 'claude-sonnet-5-5'],
  custom_llm: [],
};
const LANGS = [['en-US', 'English (US)'], ['en-GB', 'English (UK)'], ['en-AU', 'English (Australia)'], ['es', 'Spanish'], ['fr', 'French'],
  ['de', 'German'], ['it', 'Italian'], ['pt', 'Portuguese'], ['nl', 'Dutch'], ['hi', 'Hindi'], ['ur', 'Urdu'], ['ar', 'Arabic']];
const CARRIERS = { sip: 'SIP trunk', telnyx: 'Telnyx API', twilio: 'Twilio API' };

const TEMPLATES = [
  { id: 'appointment', name: 'Appointment setter', desc: 'Books a meeting or demo with interested leads.',
    first: 'Hi {{first_name}}, this is Ava from {{company_name}}. Do you have a quick minute?',
    prompt: 'You are Ava, a friendly assistant for {{company_name}}. You are calling {{first_name}} to book a short demo.\n'
      + '1. Confirm you are speaking with {{first_name}}.\n2. In one sentence, explain why you are calling.\n'
      + '3. Ask if they would like a 20-minute demo this week and agree on a day and time.\n'
      + '4. If they are busy, offer a callback at a better time.\nKeep every answer short and natural. Never invent prices or promises.',
    fields: 'meeting_time: the agreed day and time\nemail: where to send the invite' },
  { id: 'qualify', name: 'Lead qualifier', desc: 'Asks a few questions and scores interest.',
    first: 'Hi {{first_name}}, it\'s Sam from {{company_name}} – you showed interest in our service. Is now a good time?',
    prompt: 'You are Sam from {{company_name}}. Find out if {{first_name}} is a good fit.\nAsk, one at a time: what they need, '
      + 'their budget, and when they want to start. If they are a good fit and want to talk to someone, transfer them to a human. '
      + 'Otherwise thank them and end the call politely.',
    fields: 'need: what they are looking for\nbudget: their budget\ntimeline: when they want to start' },
  { id: 'receptionist', name: 'Receptionist', desc: 'Answers incoming calls, takes messages, routes callers.',
    first: 'Thanks for calling {{company_name}}, this is Mia. How can I help you today?',
    prompt: 'You are Mia, the receptionist at {{company_name}}. Help callers with general questions, take a message '
      + '(name, number, reason) when needed, and transfer them to a human for sales or urgent issues. Be warm and brief.',
    fields: 'caller_name: the caller\'s name\nreason: why they are calling\ncallback_number: best number to reach them' },
  { id: 'survey', name: 'Customer survey', desc: 'Collects feedback in under two minutes.',
    first: 'Hi {{first_name}}, this is Leo from {{company_name}}. Could I ask you two quick questions about your experience?',
    prompt: 'You are Leo from {{company_name}}. Ask {{first_name}}: 1) how satisfied they are from 1 to 10, '
      + '2) what we could do better. Thank them and end the call. Do not sell anything.',
    fields: 'rating: satisfaction from 1 to 10\nfeedback: what we could do better' },
  { id: 'reminder', name: 'Reminder & follow-up', desc: 'Confirms an appointment or follows up on a quote.',
    first: 'Hi {{first_name}}, this is a quick call from {{company_name}} about your upcoming appointment.',
    prompt: 'You are calling {{first_name}} on behalf of {{company_name}} to confirm their appointment. Ask if they can still make it. '
      + 'If not, offer to reschedule and note the new preferred time.',
    fields: 'confirmed: yes or no\nnew_time: preferred new time if rescheduling' },
  { id: 'blank', name: 'Start from scratch', desc: 'An empty agent you write yourself.', first: '', prompt: '', fields: '' },
];

const DEFAULTS = { llm_provider: 'gemini', llm_model: 'gemini-2.5-flash', tts_provider: 'elevenlabs', carrier: 'sip',
  language: 'en-US', max_minutes: 10, silence_seconds: 12, transfer_to: 'sip:agents', el_phone_type: 'sip_trunk' };

let cache = { voices: null, numbers: null, company: '' };

export async function viewAgents(el, ctx) {
  if (ctx.sub) return viewEditor(el, ctx, ctx.sub);
  el.innerHTML = `${pageHead('AI agents', 'Voice agents that call your leads and answer your numbers.',
    '<a class="btn primary" href="#/agents/new">+ New agent</a>')}<div id="alist"><div class="empty">Loading…</div></div>`;
  const d = await api('/ai-agents').catch(fail);
  const box = $('#alist');
  if (!d || !box) return;
  if (!d.items.length) {
    box.innerHTML = `<div class="card empty"><b>Create your first AI agent</b>Pick a template – appointment setter, lead qualifier,
      receptionist… – and it is ready to call in a minute.<br><br><a class="btn primary" href="#/agents/new">+ New agent</a></div>`;
    return;
  }
  box.innerHTML = `<div class="cards">${d.items.map((a) => {
    const c = a.config;
    const meta = a.kind === 'custom'
      ? `${esc(c.llm_model || c.llm_provider || '')} · ${esc(c.language || 'en-US')} · ${esc(CARRIERS[c.carrier || 'sip'] || c.carrier)}`
      : a.kind === 'elevenlabs' ? 'ElevenLabs hosted agent' : 'Telnyx AI Assistant';
    const purpose = (c.first_message || c.prompt || '').split('\n')[0];
    return `<div class="card agent-card">
      <div class="top"><div class="bot">${icon('agents')}</div>
        <div style="flex:1;min-width:0"><b>${esc(a.name)}</b><div class="meta">${meta}</div></div>
        <span class="pill" data-ready="${a.id}">checking…</span></div>
      <div class="muted small" style="min-height:2.6em">${esc(purpose.slice(0, 140))}</div>
      <div class="row">
        <button class="btn small green" data-test="${a.id}">${icon('calls')} Test call</button>
        <a class="btn small" href="#/agents/${a.id}">Edit</a>
        <button class="btn small ghost" data-dup="${a.id}">Duplicate</button>
      </div></div>`;
  }).join('')}</div>`;
  box.querySelectorAll('[data-test]').forEach((b) => { b.onclick = () => testCall(d.items.find((a) => a.id === Number(b.dataset.test))); });
  box.querySelectorAll('[data-dup]').forEach((b) => {
    b.onclick = async () => {
      const a = d.items.find((x) => x.id === Number(b.dataset.dup));
      try { await api('/ai-agents', { method: 'POST', body: { name: a.name + ' (copy)', kind: a.kind, config: a.config } }); viewAgents(el, ctx); }
      catch (e) { fail(e); }
    };
  });
  d.items.forEach((a) => api(`/ai-agents/${a.id}/check`).then((r) => {
    const p = box.querySelector(`[data-ready="${a.id}"]`);
    if (!p) return;
    p.className = 'pill ' + (r.ready ? 'ready' : 'connecting');
    p.textContent = r.ready ? 'Ready' : 'Needs setup';
    p.title = r.problems.join('\n');
  }).catch(() => {}));
  ctx.setRefresh(() => {});
}

export function testCall(a) {
  modal(`<h2>Test ${esc(a.name)}</h2><p class="muted">The agent calls you now. Afterwards the call is in Calls with its transcript and AI summary.</p>
    <label>Your phone number <input id="tnum" placeholder="+44 7700 900123" inputmode="tel"></label><p class="error" id="terr"></p>
    <div class="modal-actions"><button class="btn ghost" id="tclose">Cancel</button><button class="btn green" id="tgo">${icon('calls')} Call me</button></div>`, (m) => {
    const inp = $('#tnum', m);
    try { inp.value = localStorage.getItem('test-number') || ''; } catch { /* */ }
    inp.focus();
    $('#tclose', m).onclick = closeModal;
    $('#tgo', m).onclick = async () => {
      try {
        await api(`/ai-agents/${a.id}/test-call`, { method: 'POST', body: { number: inp.value } });
        try { localStorage.setItem('test-number', inp.value); } catch { /* */ }
        closeModal(); toast('Calling you…');
      } catch (e) { $('#terr', m).textContent = e.message; }
    };
  });
}

async function loadLookups() {
  if (!cache.voices) cache.voices = await api('/admin/elevenlabs/voices').then((r) => r.items).catch(() => []);
  if (!cache.numbers) {
    const s = await api('/admin/settings').catch(() => ({}));
    cache.numbers = s._numbers || [];
    cache.company = s.company_name || '';
  }
}

const sec = (title, sub, body) => `<div class="section"><div class="sec-title">${title}${sub ? ` <small>${sub}</small>` : ''}</div>${body}</div>`;

async function viewEditor(el, ctx, sub) {
  const isNew = sub === 'new';
  el.innerHTML = '<div class="empty">Loading…</div>';
  let a = { name: '', kind: 'custom', config: { ...DEFAULTS } };
  if (!isNew) {
    const d = await api('/ai-agents').catch(fail);
    a = d && d.items.find((x) => x.id === Number(sub));
    if (!a) { el.innerHTML = '<div class="empty">Agent not found</div>'; return; }
    a.config = { ...DEFAULTS, ...a.config };
  }
  await loadLookups();
  const c = a.config;
  const fieldsText = Array.isArray(c.fields) ? c.fields.map((f) => `${f.name}: ${f.description || ''}`).join('\n') : (c.fields || '');
  const voiceOpts = cache.voices.length
    ? `<select name="voice_id">${options([['', '— choose a voice —'], ...cache.voices.map((v) => [v.id, v.name + (v.info ? ' – ' + v.info : '')])], c.voice_id)}</select>`
    : `<input name="voice_id" value="${esc(c.voice_id || '')}" placeholder="ElevenLabs voice ID (add the ElevenLabs key in Settings → Integrations to pick from a list)">`;

  el.innerHTML = `<div class="toolbar"><a href="#/agents" class="btn ghost small">${icon('back')} AI agents</a></div>
    ${pageHead(isNew ? 'New AI agent' : a.name, isNew ? 'Start from a template – you can change everything.' : 'Changes apply to the next call.',
      isNew ? '' : `<button class="btn green" id="atest">${icon('calls')} Test call</button>`)}
    <div id="ready"></div>
    <form id="aform">
      ${isNew ? sec('Template', '', `<div class="templates">${TEMPLATES.map((t) => `<button type="button" class="template" data-tpl="${t.id}"><b>${esc(t.name)}</b><span>${esc(t.desc)}</span></button>`).join('')}</div>`) : ''}
      <div class="section"><div class="sec-title">Basics</div><div class="grid2">
        <label>Name <input name="name" value="${esc(a.name)}" required placeholder="e.g. Ava – demo booking"></label>
        <label>Agent engine <select name="kind">${options(ENGINES, a.kind)}</select></label></div></div>
      <div data-kind="custom">
        ${sec('What it says', '{{first_name}}, {{name}}, {{company}} are filled in per contact', `
          <label>First sentence when the call connects <input name="first_message" value="${esc(c.first_message || '')}"></label>
          <label>Instructions <textarea name="prompt" rows="8" placeholder="Who the agent is, its goal, and how it should talk.">${esc(c.prompt || '')}</textarea></label>
          <label>Information to collect <span class="hint">One per line – "name: description". Saved to the contact after the call.</span>
            <textarea name="fields" rows="3" placeholder="budget: monthly budget\ntimeline: when they want to start">${esc(fieldsText)}</textarea></label>`)}
        ${sec('Voice', '', `<div class="voice-row"><label>Voice ${voiceOpts}</label>
            <button type="button" class="btn" id="preview" style="margin-bottom:12px">${icon('play')} Preview</button></div>
          <div class="grid2"><label>Language <select name="language">${options(LANGS.some(([v]) => v === c.language) ? LANGS : [...LANGS, [c.language, c.language]], c.language)}</select></label></div>
          <audio id="previewAudio" hidden></audio>`)}
        ${sec('Brain', 'fast models answer in under a second', `<div class="grid2">
          <label>Model provider <select name="llm_provider">${options(LLMS, c.llm_provider)}</select></label>
          <label>Model <input name="llm_model" list="models" value="${esc(c.llm_model || '')}"><datalist id="models"></datalist>
            <span class="hint" id="modelHint"></span></label></div>`)}
        ${sec('Phone', '', `<div class="grid2">
          <label>Calls go out through <select name="carrier">${options([['sip', 'Your SIP trunk (recommended)'], ['telnyx', 'Telnyx Call Control API'], ['twilio', 'Twilio API']], c.carrier)}</select></label>
          <label>Caller ID <input name="from_number" list="nums" value="${esc(c.from_number || '')}" placeholder="Default number of the trunk">
            <datalist id="nums">${cache.numbers.map((n) => `<option value="${esc(n.number)}">${esc(n.label)}</option>`).join('')}</datalist></label>
          <label>Hand hot leads to <input name="transfer_to" value="${esc(c.transfer_to || '')}" placeholder="sip:agents">
            <span class="hint">sip:agents = your team in this CRM, or a +number. Empty = offer a callback instead.</span></label></div>`)}
        <details class="section"><summary>Advanced</summary><div class="grid2">
          <label>Speech-to-text <select name="stt_provider">${options([['', 'Default (Settings)'], ['assemblyai', 'AssemblyAI'], ['deepgram', 'Deepgram']], c.stt_provider)}</select></label>
          <label>ElevenLabs model <input name="tts_model" value="${esc(c.tts_model || '')}" placeholder="eleven_flash_v2_5 (fastest)"></label>
          <label>Longest call (minutes) <input name="max_minutes" type="number" min="1" value="${esc(c.max_minutes)}"></label>
          <label>Hang up after silence (seconds) <input name="silence_seconds" type="number" min="4" value="${esc(c.silence_seconds)}"></label>
          <label>Voice engine <select name="tts_provider">${options([['elevenlabs', 'ElevenLabs'], ['telnyx', 'Telnyx (Telnyx API calls only)']], c.tts_provider)}</select></label></div>
          <label class="switch"><input type="checkbox" name="record" ${c.record ? 'checked' : ''}> Record calls</label></details>
      </div>
      <div data-kind="elevenlabs">${sec('ElevenLabs hosted agent', 'prompt, voice and tools are set inside ElevenLabs', `<div class="grid2">
          <label>ElevenLabs agent ID <input name="el_agent_id" value="${esc(c.el_agent_id || '')}" placeholder="agent_…"></label>
          <label>Phone number ID in ElevenLabs <input name="el_phone_number_id" value="${esc(c.el_phone_number_id || '')}" placeholder="phnum_…"></label>
          <label>Number type <select name="el_phone_type">${options([['sip_trunk', 'SIP trunk (e.g. Telnyx)'], ['twilio', 'Twilio']], c.el_phone_type)}</select></label></div>`)}</div>
      <div data-kind="telnyx">${sec('Telnyx AI Assistant', 'runs inside Telnyx – needs the Telnyx API key', `<div class="grid2">
          <label>Assistant ID <input name="tx_assistant_id" value="${esc(c.tx_assistant_id || '')}" placeholder="assistant-…"></label>
          <label>Caller ID (Telnyx number) <input name="from_number" value="${esc(c.from_number || '')}"></label></div>`)}</div>
      <p class="error" id="aerr"></p>
      <div class="toolbar" style="margin-top:4px">
        <button class="btn primary">${isNew ? 'Create agent' : 'Save changes'}</button>
        <a class="btn ghost" href="#/agents">Cancel</a>
        ${isNew ? '' : '<button type="button" class="btn red ghost" id="adel" style="margin-left:auto">Delete agent</button>'}
      </div>
    </form>`;

  const f = $('#aform');
  const fld = (n) => f.elements.namedItem(n);
  const kind = fld('kind');
  const sync = () => f.querySelectorAll('[data-kind]').forEach((x) => {
    x.hidden = x.dataset.kind !== kind.value;
    x.querySelectorAll('input, select, textarea').forEach((i) => { i.disabled = x.hidden; });
  });
  kind.onchange = sync; sync();
  const prov = fld('llm_provider');
  const models = () => {
    const list = MODELS[prov.value] || [];
    $('#models').innerHTML = list.map((m) => `<option value="${esc(m)}">`).join('');
    $('#modelHint').textContent = list.length ? `Recommended for calls: ${list[0]}` : 'Your provider\'s model name';
  };
  prov.onchange = () => { const list = MODELS[prov.value] || []; fld('llm_model').value = list[0] || ''; models(); };
  models();
  f.querySelectorAll('[data-tpl]').forEach((b) => {
    b.onclick = () => {
      const t = TEMPLATES.find((x) => x.id === b.dataset.tpl);
      const fill = (s) => s.replace(/\{\{company_name\}\}/g, cache.company || 'our company');
      f.querySelectorAll('[data-tpl]').forEach((x) => x.classList.toggle('on', x === b));
      if (!fld('name').value || TEMPLATES.some((x) => x.name === fld('name').value)) fld('name').value = t.id === 'blank' ? '' : t.name;
      fld('first_message').value = fill(t.first);
      fld('prompt').value = fill(t.prompt);
      fld('fields').value = t.fields;
      if (t.id === 'receptionist') fld('transfer_to').value = 'sip:agents';
    };
  });
  $('#preview').onclick = () => {
    const v = (cache.voices || []).find((x) => x.id === (fld('voice_id') || {}).value);
    if (!v || !v.preview) return toast('No preview for this voice', true);
    const au = $('#previewAudio'); au.src = v.preview; au.play().catch(() => {});
  };
  if (!isNew) {
    $('#atest').onclick = () => testCall(a);
    $('#adel').onclick = async () => {
      if (!confirm(`Delete ${a.name}?`)) return;
      try { await api('/ai-agents/' + a.id, { method: 'DELETE' }); location.hash = '#/agents'; } catch (e) { fail(e); }
    };
    api(`/ai-agents/${a.id}/check`).then((r) => {
      if (!r.ready && $('#ready')) $('#ready').innerHTML = `<div class="note warn" style="margin-bottom:16px"><b>Before this agent can call:</b>
        <ul style="margin:6px 0 0;padding-left:18px">${r.problems.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div>`;
    }).catch(() => {});
  }
  f.onsubmit = async (e) => {
    e.preventDefault();
    const v = {};
    f.querySelectorAll('input:not(:disabled), select:not(:disabled), textarea:not(:disabled)').forEach((i) => {
      if (i.name) v[i.name] = i.type === 'checkbox' ? i.checked : i.value;
    });
    const { name, kind: k, ...config } = v;
    try {
      const r = await api(isNew ? '/ai-agents' : '/ai-agents/' + a.id, { method: isNew ? 'POST' : 'PUT', body: { name, kind: k, config } });
      toast(isNew ? 'Agent created' : 'Saved');
      location.hash = '#/agents/' + (isNew ? r.id : a.id);
      if (!isNew) viewEditor(el, ctx, String(a.id));
    } catch (err) { $('#aerr').textContent = err.message; }
  };
  ctx.setRefresh(() => {});
}
