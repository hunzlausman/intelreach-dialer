// AI agents: our own pipeline (STT -> LLM -> TTS), ElevenLabs agents, Telnyx AI Assistants.
import { $, api, closeModal, esc, fail, modal, options, toast } from './core.js';

const KINDS = [['custom', 'Custom pipeline (AssemblyAI → LLM → ElevenLabs)'], ['elevenlabs', 'ElevenLabs agent'],
  ['telnyx', 'Telnyx AI Assistant']];
const LLMS = [['anthropic', 'Anthropic Claude'], ['openai', 'OpenAI'], ['gemini', 'Google Gemini'], ['custom_llm', 'OpenAI-compatible']];
const MODEL_HINT = { anthropic: 'claude-opus-5-5 (or claude-sonnet-5-5, claude-haiku-5-5)', openai: 'e.g. gpt-4.1-mini',
  gemini: 'e.g. gemini-2.5-flash', custom_llm: 'model name at your provider' };

export async function viewAgents(el, ctx) {
  el.innerHTML = `<div class="toolbar"><h2 style="margin:0;flex:1">AI agents</h2><button class="btn primary" id="newAgent">+ AI agent</button></div>
    <div id="alist"><div class="empty">Loading…</div></div>`;
  $('#newAgent').onclick = () => editAgent(null, load);
  async function load() {
    const d = await api('/ai-agents').catch(fail);
    const box = $('#alist');
    if (!d || !box) return;
    if (!d.items.length) {
      box.innerHTML = '<div class="card empty">No AI agents yet. Add your provider keys in Admin → Integrations first.</div>';
      return;
    }
    box.innerHTML = d.items.map((a) => {
      const c = a.config;
      const detail = a.kind === 'custom'
        ? `${esc(c.llm_provider || 'anthropic')} ${esc(c.llm_model || '')} · voice ${esc(c.tts_provider || 'elevenlabs')} · via ${esc(c.carrier || 'telnyx')}`
        : a.kind === 'elevenlabs' ? `ElevenLabs agent ${esc(c.el_agent_id)}` : `Telnyx assistant ${esc(c.tx_assistant_id)}`;
      return `<div class="card panel"><div class="toolbar" style="margin:0">
          <div style="flex:1"><b>${esc(a.name)}</b> <span class="tag">${esc((KINDS.find((k) => k[0] === a.kind) || ['', a.kind])[1])}</span>
            <div class="muted">${detail}</div></div>
          <button class="btn small" data-test="${a.id}">Test call</button><button class="btn small" data-edit="${a.id}">Edit</button>
        </div></div>`;
    }).join('');
    box.querySelectorAll('[data-edit]').forEach((b) => { b.onclick = () => editAgent(d.items.find((a) => a.id === Number(b.dataset.edit)), load); });
    box.querySelectorAll('[data-test]').forEach((b) => { b.onclick = () => testCall(d.items.find((a) => a.id === Number(b.dataset.test))); });
  }
  ctx.setRefresh(load);
  load();
}

function testCall(a) {
  modal(`<h2>Test ${esc(a.name)}</h2><p class="muted">The agent calls this number now. Afterwards the call shows up in Calls with its transcript and summary.</p>
    <label>Your phone number <input id="tnum" placeholder="+44 7700 900123"></label><p class="error" id="terr"></p>
    <div class="modal-actions"><button class="btn ghost" id="tclose">Close</button><button class="btn green" id="tgo">Call me</button></div>`, (m) => {
    $('#tclose', m).onclick = closeModal;
    $('#tgo', m).onclick = async () => {
      try { await api(`/ai-agents/${a.id}/test-call`, { method: 'POST', body: { number: $('#tnum', m).value } }); closeModal(); toast('Calling…'); }
      catch (e) { $('#terr', m).textContent = e.message; }
    };
  });
}

async function lookup(what) {
  try { return (await api('/admin/elevenlabs/' + what)).items; } catch { return null; }
}

async function editAgent(a, done) {
  const isNew = !a;
  a = a || { name: '', kind: 'custom', config: { llm_provider: 'anthropic', llm_model: 'claude-opus-5-5', tts_provider: 'elevenlabs',
    carrier: 'telnyx', language: 'en-US', max_minutes: 10, silence_seconds: 12, el_phone_type: 'sip_trunk',
    first_message: 'Hi {{first_name}}, this is Ava from Acme. Do you have a minute?',
    prompt: 'You are Ava, a friendly sales assistant for Acme. Your goal is to find out whether {{first_name}} is interested in a demo, '
      + 'learn their budget and timeline, and book a callback with a human colleague if they are interested.' } };
  const c = a.config;
  const [voices, elAgents, elNumbers] = await Promise.all([lookup('voices'), lookup('agents'), lookup('phone-numbers')]);
  const fieldsText = (c.fields || []).map((f) => `${f.name}: ${f.description || ''}`).join('\n');
  const pick = (name, items, cur, map, placeholder) => (items && items.length
    ? `<select name="${name}">${options([['', '— choose —'], ...items.map(map)], cur)}</select>`
    : `<input name="${name}" value="${esc(cur)}" placeholder="${esc(placeholder)}">`);
  modal(`<h2>${isNew ? 'New AI agent' : 'Edit ' + esc(a.name)}</h2><form id="aform">
    <div class="grid2">
      <label>Name <input name="name" value="${esc(a.name)}" required></label>
      <label>Type <select name="kind">${options(KINDS, a.kind)}</select></label>
    </div>
    <div data-kind="custom">
      <label>Instructions (system prompt) – {{first_name}}, {{name}}, {{company}} are filled in per contact
        <textarea name="prompt" rows="6">${esc(c.prompt)}</textarea></label>
      <label>First sentence when the call is answered <input name="first_message" value="${esc(c.first_message)}"></label>
      <label>Information to collect – one per line, "name: description"
        <textarea name="fields" rows="3" placeholder="budget: monthly budget in USD&#10;timeline: when they want to start">${esc(fieldsText)}</textarea></label>
      <div class="grid2">
        <label>LLM provider <select name="llm_provider">${options(LLMS, c.llm_provider)}</select></label>
        <label>Model <input name="llm_model" value="${esc(c.llm_model)}" placeholder="${esc(MODEL_HINT[c.llm_provider || 'anthropic'])}"></label>
        <label>Voice <select name="tts_provider">${options([['elevenlabs', 'ElevenLabs'], ['telnyx', 'Telnyx (Telnyx calls only)']], c.tts_provider)}</select></label>
        <label>Voice ${pick('voice_id', voices, c.voice_id, (v) => [v.id, v.name], 'ElevenLabs voice ID')}</label>
        <label>ElevenLabs model <input name="tts_model" value="${esc(c.tts_model)}" placeholder="eleven_flash_v2_5"></label>
        <label>Language code <input name="language" value="${esc(c.language)}" placeholder="en-US"></label>
        <label>Phone carrier <select name="carrier">${options([['telnyx', 'Telnyx'], ['twilio', 'Twilio']], c.carrier)}</select></label>
        <label>Caller ID (empty = default) <input name="from_number" value="${esc(c.from_number)}" placeholder="+15551234567"></label>
        <label>Transfer hot leads to (+number or sip:) <input name="transfer_to" value="${esc(c.transfer_to)}"></label>
        <label>Longest call (minutes) <input name="max_minutes" type="number" min="1" value="${esc(c.max_minutes)}"></label>
        <label>Hang up after silence (seconds) <input name="silence_seconds" type="number" min="4" value="${esc(c.silence_seconds)}"></label>
      </div>
      <label class="switch"><input type="checkbox" name="record" ${c.record ? 'checked' : ''}> Record calls (Telnyx)</label>
    </div>
    <div data-kind="elevenlabs">
      <p class="muted">The conversation runs inside ElevenLabs (configure prompt, voice, tools and knowledge base there). Import your Telnyx
        or Twilio number into ElevenLabs → Phone numbers first. Set the post-call webhook to the URL shown in Admin → Integrations.</p>
      <div class="grid2">
        <label>ElevenLabs agent ${pick('el_agent_id', elAgents, c.el_agent_id, (x) => [x.id, x.name || x.id], 'agent_…')}</label>
        <label>Phone number in ElevenLabs ${pick('el_phone_number_id', elNumbers, c.el_phone_number_id, (x) => [x.id, `${x.number} (${x.provider})`], 'phnum_…')}</label>
        <label>Number type <select name="el_phone_type">${options([['sip_trunk', 'SIP trunk (e.g. Telnyx)'], ['twilio', 'Twilio']], c.el_phone_type)}</select></label>
      </div>
    </div>
    <div data-kind="telnyx">
      <p class="muted">The conversation runs inside Telnyx (Portal → AI → Assistants). The CRM dials with Telnyx Call Control
        and starts the assistant when the call is answered.</p>
      <div class="grid2">
        <label>Assistant ID <input name="tx_assistant_id" value="${esc(c.tx_assistant_id)}" placeholder="assistant-…"></label>
        <label>Caller ID (Telnyx number) <input name="from_number" value="${esc(c.from_number)}"></label>
      </div>
      <label class="switch"><input type="checkbox" name="record" ${c.record ? 'checked' : ''}> Record calls</label>
    </div>
    <p class="error" id="aerr"></p>
    <div class="modal-actions">${!isNew ? '<button type="button" class="btn red ghost" id="adel" style="margin-right:auto">Delete</button>' : ''}
      <button type="button" class="btn ghost" id="acancel">Cancel</button><button class="btn primary">Save</button></div>
  </form>`, (m) => {
    const kind = m.querySelector('[name=kind]');
    const sync = () => m.querySelectorAll('[data-kind]').forEach((x) => {
      x.hidden = x.dataset.kind !== kind.value;
      x.querySelectorAll('input, select, textarea').forEach((i) => { i.disabled = x.hidden; });
    });
    kind.onchange = sync; sync();
    const prov = m.querySelector('[name=llm_provider]');
    prov.onchange = () => { m.querySelector('[name=llm_model]').placeholder = MODEL_HINT[prov.value]; };
    $('#acancel', m).onclick = closeModal;
    if (!isNew) $('#adel', m).onclick = async () => {
      if (!confirm('Delete this AI agent?')) return;
      try { await api('/ai-agents/' + a.id, { method: 'DELETE' }); closeModal(); done(); } catch (e) { fail(e); }
    };
    $('#aform', m).onsubmit = async (e) => {
      e.preventDefault();
      const f = {};
      e.target.querySelectorAll('input:not(:disabled), select:not(:disabled), textarea:not(:disabled)').forEach((i) => {
        if (i.name) f[i.name] = i.type === 'checkbox' ? i.checked : i.value;
      });
      const { name, kind: k, ...config } = f;
      try {
        await api(isNew ? '/ai-agents' : '/ai-agents/' + a.id, { method: isNew ? 'POST' : 'PUT', body: { name, kind: k, config } });
        closeModal(); toast('Saved'); done();
      } catch (err) { $('#aerr', m).textContent = err.message; }
    };
  }, true);
}

