// Browser softphone: registers the agent's extension (2001+) with the
// IntelReach Asterisk over WSS and places / receives PSTN calls through it.
// ICE / SDP handling follows the working IntelReach phone (web/src/sip).

const KEEP = ['opus', 'g722', 'pcmu', 'pcma', 'telephone-event'];

// Asterisk rejects offers with too many codecs – keep only what we use.
function filterSdp(sdp) {
  const lines = sdp.split(/\r?\n/);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].startsWith('m=')) { out.push(lines[i]); i++; continue; }
    const sec = [lines[i]]; i++;
    while (i < lines.length && !lines[i].startsWith('m=')) { sec.push(lines[i]); i++; }
    const m = sec[0].split(' ');
    const names = {};
    sec.forEach((l) => { const r = l.match(/^a=rtpmap:(\d+) ([^/]+)/); if (r) names[r[1]] = r[2].toLowerCase(); });
    let keep = m.slice(3).filter((pt) => KEEP.includes(names[pt]));
    if (!keep.length) keep = m.slice(3);
    const set = new Set(keep);
    out.push(m.slice(0, 3).concat(keep).join(' '));
    sec.slice(1).forEach((l) => {
      const r = l.match(/^a=(rtpmap|fmtp|rtcp-fb):(\d+)[ ]/);
      if (!r || set.has(r[2])) out.push(l);
    });
  }
  return out.join('\r\n');
}

// simple ring tones with WebAudio
let audioCtx;
function tone(freq, on, off) {
  let stopped = false, timer;
  const play = () => {
    if (stopped) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = freq; g.gain.value = 0.08;
      o.connect(g).connect(audioCtx.destination);
      o.start(); o.stop(audioCtx.currentTime + on);
    } catch { /* autoplay blocked until the first click */ }
    timer = setTimeout(play, (on + off) * 1000);
  };
  play();
  return () => { stopped = true; clearTimeout(timer); };
}

function friendly(e) {
  const code = e && e.message && e.message.status_code;
  const cause = (e && e.cause) || '';
  if (code === 403 || cause === 'Rejected') return 'Call not allowed (country blocked or no caller ID)';
  if (code === 486 || cause === 'Busy') return 'Busy';
  if (code === 404 || code === 484 || cause === 'Not Found') return 'Number not reachable';
  if (code === 480 || cause === 'Unavailable') return 'Unavailable';
  if (code === 487 || cause === 'Canceled') return 'Cancelled';
  if (cause === 'No Answer' || code === 408) return 'No answer';
  if (cause === 'User Denied Media Access') return 'Microphone blocked – allow it in the browser';
  return cause || 'Call failed';
}

export class Phone {
  constructor(audioEl) {
    this.audio = audioEl;
    this.ua = null;
    this.sip = null;
    this.state = 'off';          // off | connecting | ready | error
    this.error = '';
    this.call = null;            // { dir, number, name, status, startedAt, muted, held, callId, reason }
    this.session = null;
    this.listeners = new Set();
    this.stopTone = null;
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit() { this.listeners.forEach((fn) => fn(this)); }
  setCall(patch) { this.call = this.call ? { ...this.call, ...patch } : patch; this.emit(); }

  start(sip) {
    this.sip = sip;
    if (!window.JsSIP) { this.state = 'error'; this.error = 'JsSIP did not load'; this.emit(); return; }
    this.stop();
    this.state = 'connecting'; this.error = ''; this.emit();
    const ua = new window.JsSIP.UA({
      sockets: [new window.JsSIP.WebSocketInterface(sip.wss)],
      uri: `sip:${sip.ext}@${sip.domain}`,
      password: sip.password,
      display_name: sip.ext,
      session_timers: false,
      register_expires: 300,
    });
    ua.on('connected', () => { this.state = 'connecting'; this.emit(); });
    ua.on('disconnected', () => { this.state = 'connecting'; this.error = 'Disconnected – retrying…'; this.emit(); });
    ua.on('registered', () => { this.state = 'ready'; this.error = ''; this.emit(); });
    ua.on('unregistered', () => { if (this.state === 'ready') { this.state = 'connecting'; this.emit(); } });
    ua.on('registrationFailed', (e) => { this.state = 'error'; this.error = 'Phone line refused: ' + e.cause; this.emit(); });
    ua.on('newRTCSession', ({ session, originator, request }) => {
      if (originator !== 'remote') return;
      if (this.session) { session.terminate({ status_code: 486 }); return; }   // already on a call
      const ident = session.remote_identity;
      this.session = session;
      this.wire(session);
      this.stopTone = tone(520, 0.9, 1.6);
      this.call = null;
      this.setCall({
        dir: 'in', status: 'ringing', number: ident.uri.user, name: ident.display_name || '',
        callId: Number(request.getHeader('X-CRM-Call')) || null, muted: false, held: false, startedAt: null,
      });
      if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
        try { new Notification('Incoming call', { body: ident.display_name || ident.uri.user }); } catch { /* */ }
      }
    });
    this.ua = ua;
    ua.start();
  }

  stop() {
    if (this.ua) { try { this.ua.stop(); } catch { /* */ } }
    this.ua = null;
    this.state = 'off';
  }

  pcConfig() {
    return { iceServers: this.sip.iceServers || [], bundlePolicy: 'max-bundle', rtcpMuxPolicy: 'require' };
  }

  dial(number, { callId = null, name = '' } = {}) {
    if (this.state !== 'ready') throw new Error('Phone line is not connected yet');
    if (this.session) throw new Error('You are already on a call');
    const opts = {
      mediaConstraints: { audio: true, video: false },
      pcConfig: this.pcConfig(),
      rtcOfferConstraints: { offerToReceiveAudio: true, offerToReceiveVideo: false },
      extraHeaders: callId ? [`X-CRM-Call: ${callId}`] : [],
    };
    this.call = null;
    this.setCall({ dir: 'out', status: 'calling', number, name, callId, muted: false, held: false, startedAt: null });
    const s = this.ua.call(`sip:${number}@${this.sip.domain}`, opts);
    this.session = s;
    this.wire(s);
  }

  wire(s) {
    s.on('sdp', (e) => { if (e.originator === 'local') e.sdp = filterSdp(e.sdp); });
    let iceTimer = null;
    s.on('icecandidate', (e) => {
      const c = (e.candidate && e.candidate.candidate) || '';
      if (c.includes(' typ srflx') || c.includes(' typ relay')) e.ready();
      if (!iceTimer) iceTimer = setTimeout(() => e.ready(), 2000);
    });
    const attach = (pc) => {
      pc.addEventListener('track', (ev) => {
        this.audio.srcObject = ev.streams[0] || new MediaStream([ev.track]);
        this.audio.play().catch(() => {});
      });
    };
    if (s.connection) attach(s.connection);
    s.on('peerconnection', (e) => attach(e.peerconnection));
    s.on('progress', (e) => {
      if (!this.call || this.call.dir !== 'out') return;
      if (this.call.status !== 'ringing') this.setCall({ status: 'ringing' });
      // 183 with SDP = the far end's own ringback / announcements: play that instead of ours
      const early = e && e.response && e.response.status_code === 183 && e.response.body;
      if (early) this.quiet();
      else if (!this.stopTone) this.stopTone = tone(425, 1, 3);
    });
    s.on('accepted', () => { this.quiet(); this.setCall({ status: 'active', startedAt: Date.now() }); });
    s.on('confirmed', () => { this.quiet(); if (this.call && !this.call.startedAt) this.setCall({ status: 'active', startedAt: Date.now() }); });
    s.on('hold', () => this.setCall({ held: true }));
    s.on('unhold', () => this.setCall({ held: false }));
    s.on('ended', () => this.finish('ended', ''));
    s.on('failed', (e) => this.finish(this.call && this.call.dir === 'in' && this.call.status === 'ringing' ? 'missed' : 'failed', friendly(e)));
  }

  quiet() { if (this.stopTone) { this.stopTone(); this.stopTone = null; } }

  finish(status, reason) {
    this.quiet();
    this.session = null;
    this.audio.srcObject = null;
    const ended = this.call ? { ...this.call, status, reason, endedAt: Date.now() } : null;
    this.call = ended;
    this.emit();
  }

  clear() { if (!this.session) { this.call = null; this.emit(); } }

  answer() {
    if (!this.session || !this.call || this.call.dir !== 'in') return;
    this.quiet();
    this.session.answer({ mediaConstraints: { audio: true, video: false }, pcConfig: this.pcConfig() });
    this.setCall({ status: 'connecting' });
  }

  hangup() {
    if (!this.session) return;
    try { this.session.terminate(); } catch { this.finish('ended', ''); }
  }

  toggleMute() {
    if (!this.session || !this.call) return;
    if (this.call.muted) this.session.unmute({ audio: true }); else this.session.mute({ audio: true });
    this.setCall({ muted: !this.call.muted });
  }

  toggleHold() {
    if (!this.session || !this.call || this.call.status !== 'active') return;
    if (this.call.held) this.session.unhold(); else this.session.hold();
  }

  dtmf(d) {
    if (this.session && this.call && this.call.status === 'active') {
      try { this.session.sendDTMF(d, { transportType: 'RFC2833' }); } catch { /* */ }
    }
  }
}
