// Live captions + AI tips for agents on human calls.
// The browser streams both sides of the call (agent mic, remote audio) to
// AssemblyAI with a short-lived token, shows the words live, asks the CRM's LLM
// for a tip after each thing the contact says, and saves the transcript at the end.
import { $, api, esc } from './core.js';

const RATE = 16000;
let current = null;

function downsample(input, fromRate) {
  const ratio = fromRate / RATE;
  const out = new Int16Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

class Side {
  constructor(ctx, stream, role, token, onTurn, onPartial) {
    this.role = role;
    this.buf = [];
    this.ws = new WebSocket(`wss://streaming.assemblyai.com/v3/ws?sample_rate=${RATE}&encoding=pcm_s16le&token=${encodeURIComponent(token)}`);
    this.ws.onmessage = (m) => {
      let d; try { d = JSON.parse(m.data); } catch { return; }
      if (d.type !== 'Turn' || !d.transcript) return;
      if (d.end_of_turn) onTurn(role, d.transcript); else onPartial(role, d.transcript);
    };
    this.src = ctx.createMediaStreamSource(stream);
    this.node = new AudioWorkletNode(ctx, 'pcm-capture');
    this.node.port.onmessage = (e) => {
      const pcm = downsample(e.data, ctx.sampleRate);
      this.buf.push(pcm);
      const total = this.buf.reduce((n, a) => n + a.length, 0);
      if (total >= RATE / 10 && this.ws.readyState === 1) {          // send every 100 ms
        const all = new Int16Array(total);
        let o = 0; this.buf.forEach((a) => { all.set(a, o); o += a.length; });
        this.buf = [];
        this.ws.send(all.buffer);
      }
    };
    this.src.connect(this.node);
  }

  stop() {
    try { this.src.disconnect(); this.node.disconnect(); } catch { /* */ }
    try { if (this.ws.readyState === 1) this.ws.send(JSON.stringify({ type: 'Terminate' })); this.ws.close(); } catch { /* */ }
  }
}

export async function startCaptions(session, callId) {
  stopCaptions();
  const box = $('#assistPanel');
  const pc = session && session.connection;
  if (!pc) return;
  const mic = pc.getSenders().map((s) => s.track).filter((t) => t && t.kind === 'audio');
  const remote = pc.getReceivers().map((r) => r.track).filter((t) => t && t.kind === 'audio');
  if (!mic.length || !remote.length) return;
  let token;
  try { token = (await api('/ai/stt-token')).token; } catch (e) {
    box.hidden = false; box.innerHTML = `<div class="muted">Live captions unavailable: ${esc(e.message)}</div>`; return;
  }
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.audioWorklet.addModule('/pcm-worklet.js');
  const state = { callId, ctx, lines: [], partial: {}, tip: '', lastTip: 0, sides: [] };
  current = state;
  box.hidden = false;
  const render = () => {
    if (current !== state) return;
    const rows = state.lines.slice(-12).map((l) => `<div class="cap ${l.role}"><b>${l.role === 'agent' ? 'You' : 'Contact'}:</b> ${esc(l.text)}</div>`);
    Object.entries(state.partial).forEach(([role, text]) => {
      if (text) rows.push(`<div class="cap ${role} partial"><b>${role === 'agent' ? 'You' : 'Contact'}:</b> ${esc(text)}</div>`);
    });
    box.innerHTML = `<div class="assist-head">Live captions</div><div class="caps">${rows.join('') || '<span class="muted">Listening…</span>'}</div>
      ${state.tip ? `<div class="tip"><b>AI tip:</b> ${esc(state.tip)}</div>` : ''}`;
    const caps = box.querySelector('.caps'); caps.scrollTop = caps.scrollHeight;
  };
  const onPartial = (role, text) => { state.partial[role] = text; render(); };
  const onTurn = (role, text) => {
    state.partial[role] = '';
    state.lines.push({ role, text });
    render();
    if (role === 'contact' && Date.now() - state.lastTip > 8000) {
      state.lastTip = Date.now();
      const transcript = state.lines.map((l) => `${l.role}: ${l.text}`).join('\n');
      api('/ai/tips', { method: 'POST', body: { call_id: callId, transcript } })
        .then((r) => { state.tip = r.tip; render(); }).catch(() => {});
    }
  };
  state.sides.push(new Side(ctx, new MediaStream(mic), 'agent', token, onTurn, onPartial));
  state.sides.push(new Side(ctx, new MediaStream(remote), 'contact', token, onTurn, onPartial));
  render();
}

export function stopCaptions() {
  const s = current;
  current = null;
  const box = $('#assistPanel');
  if (box) { box.hidden = true; box.innerHTML = ''; }
  if (!s) return;
  s.sides.forEach((x) => x.stop());
  try { s.ctx.close(); } catch { /* */ }
  if (s.callId && s.lines.length) {
    api(`/calls/${s.callId}/transcript`, { method: 'POST', body: { items: s.lines } }).catch(() => {});
  }
}
