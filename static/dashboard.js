// Dashboard: KPI tiles, calls-per-day chart, outcomes, live calls, callbacks, campaign & agent leaderboards.
import { $, api, dur, esc, fail, icon, outcomeLabel, pageHead, session, STATUS_LABEL, store, when } from './core.js';

const RANGES = [[1, 'Today'], [7, '7 days'], [30, '30 days'], [90, '90 days']];
const fmtDay = (iso) => new Date(iso + 'T12:00:00Z').toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' });

function delta(cur, prev, suffix = '') {
  if (!prev && !cur) return '<small>no calls in the previous period</small>';
  if (!prev) return '<small>new this period</small>';
  const d = Math.round(((cur - prev) / prev) * 100);
  if (!d) return '<small>same as the previous period</small>';
  return `<small><span class="delta ${d > 0 ? 'up' : 'down'}">${d > 0 ? '▲' : '▼'} ${Math.abs(d)}%${suffix}</span> vs previous period</small>`;
}

function tiles(k, p) {
  const t = (label, value, foot) => `<div class="card stat"><span>${label}</span><b>${value}</b>${foot}</div>`;
  return `<div class="stats">
    ${t('Calls', k.calls.toLocaleString(), delta(k.calls, p.calls))}
    ${t('Connect rate', k.calls ? k.connect_rate + '%' : '—', `<small>${k.connected} connected · ${k.missed} missed</small>`)}
    ${t('Avg talk time', k.connected ? dur(k.avg_talk) : '—', `<small>${dur(k.talk_seconds)} in total</small>`)}
    ${t('Positive outcomes', k.positive, delta(k.positive, p.positive))}
    ${t('AI calls', k.ai_calls, `<small>${k.human_calls} by people · ${k.calls ? Math.round(100 * k.ai_calls / k.calls) : 0}% AI</small>`)}
    ${t('Avg lead score', k.avg_score ?? '—', '<small>from AI call analysis</small>')}
  </div>`;
}

// Stacked bars (connected / other) – thin marks, 4px rounded tops, 2px surface gap, hover tooltip.
function drawChart(box, daily) {
  const W = Math.max(280, box.clientWidth), H = 220, L = 32, B = 22, T = 8;
  const max = Math.max(4, ...daily.map((d) => d.calls));
  const step = Math.ceil(max / 4);
  const top = step * 4;
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const band = (W - L) / daily.length;
  const bw = Math.max(3, Math.min(24, band * 0.6));
  const every = Math.ceil(daily.length / Math.max(1, Math.floor((W - L) / 64)));
  const round = (x, y0, w, h) => {      // rect with 4px rounded top corners, square at the baseline
    if (h <= 0) return '';
    const r = Math.min(4, w / 2, h);
    return `M${x},${y0 + h}V${y0 + r}Q${x},${y0} ${x + r},${y0}H${x + w - r}Q${x + w},${y0} ${x + w},${y0 + r}V${y0 + h}Z`;
  };
  let marks = '', grid = '', labels = '', hits = '';
  for (let i = 0; i <= 4; i++) {
    const v = step * i;
    grid += `<line class="grid" x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  daily.forEach((d, i) => {
    const x = L + band * i + (band - bw) / 2;
    const other = d.calls - d.connected;
    const yc = y(d.connected), yt = y(d.calls);
    const gap = d.connected && other ? 2 : 0;
    if (d.connected) marks += `<path class="s1" d="${d.calls === d.connected ? round(x, yc, bw, y(0) - yc) : `M${x},${y(0)}V${yc}H${x + bw}V${y(0)}Z`}"/>`;
    if (other) marks += `<path class="s0" d="${round(x, yt, bw, yc - yt - gap)}"/>`;
    if (i % every === 0) labels += `<text class="axis" x="${x + bw / 2}" y="${H - 4}" text-anchor="middle">${fmtDay(d.day)}</text>`;
    hits += `<rect class="hit" data-i="${i}" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}"/>`;
  });
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Calls per day">${grid}${hits}${marks}${labels}</svg>
    <div class="tooltip" hidden></div>`;
  const tip = box.querySelector('.tooltip');
  box.querySelectorAll('.hit').forEach((r) => {
    r.onmouseenter = () => {
      const d = daily[Number(r.dataset.i)];
      tip.innerHTML = `<b>${fmtDay(d.day)}</b><br>${d.calls} calls · ${d.connected} connected${d.ai ? ` · ${d.ai} AI` : ''}`;
      tip.style.left = (Number(r.getAttribute('x')) + band / 2) + 'px';
      tip.style.top = y(d.calls) + 'px';
      tip.hidden = false;
    };
    r.onmouseleave = () => { tip.hidden = true; };
  });
}

export async function viewDashboard(el, ctx) {
  const days = Number(store.get('dash-days')) || 7;
  const first = session.me.user.name.split(' ')[0];
  const hour = new Date().getHours();
  const hello = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  el.innerHTML = `${pageHead(`${hello}, ${first}`, 'Here is how your calls are going.',
      `<div class="seg" id="range">${RANGES.map(([v, l]) => `<button data-d="${v}" class="${v === days ? 'on' : ''}">${l}</button>`).join('')}</div>`)}
    <div id="dash"><div class="empty">Loading…</div></div>`;
  el.querySelectorAll('#range button').forEach((b) => {
    b.onclick = () => { store.set('dash-days', b.dataset.d); viewDashboard(el, ctx); };
  });
  let d;
  try { d = await api(`/dashboard?days=${days}&tz=${-new Date().getTimezoneOffset()}`); } catch (e) { fail(e); return; }
  const box = $('#dash');
  if (!box) return;
  const k = d.kpis;
  const maxOut = Math.max(1, ...d.outcomes.map((o) => o.n));
  box.innerHTML = `${tiles(k, d.prev)}
    <div class="dash-grid">
      <div class="card">
        <div class="card-head"><h2>Calls per day</h2>
          <div class="legend"><span><i style="background:var(--series-1)"></i>Connected</span><span><i style="background:var(--series-quiet)"></i>Not connected</span></div></div>
        <div class="chart" id="chart"></div>
        <details style="margin-top:8px"><summary class="muted small" style="cursor:pointer">Show as table</summary>
          <div class="table-wrap"><table><thead><tr><th>Day</th><th class="num">Calls</th><th class="num">Connected</th><th class="num">AI</th></tr></thead>
          <tbody>${d.daily.map((x) => `<tr><td>${fmtDay(x.day)}</td><td class="num">${x.calls}</td><td class="num">${x.connected}</td><td class="num">${x.ai}</td></tr>`).join('')}</tbody></table></div>
        </details>
      </div>
      <div class="card">
        <div class="card-head"><h2>Live now</h2><span class="pill ${d.live.length ? 'ready' : 'plain'}">${d.live.length} on calls</span></div>
        ${d.live.length ? d.live.map((c) => `<div class="live-row"><span class="live-dot"></span>
            <div style="flex:1;min-width:0"><a href="#/calls/${c.id}">${esc(c.contact_name || c.number)}</a>
              <div class="muted small">${c.ai_agent_name ? 'AI · ' + esc(c.ai_agent_name) : esc(c.agent_name || (c.direction === 'in' ? 'Incoming' : ''))} · ${esc(STATUS_LABEL[c.status] || c.status)}</div></div>
            <span class="muted small">${when(c.started_at)}</span></div>`).join('')
          : '<div class="empty" style="padding:24px 8px">No calls in progress.</div>'}
        <div class="muted small" style="margin-top:10px">${d.online} agent${d.online === 1 ? '' : 's'} taking calls</div>
      </div>
    </div>
    <div class="dash-grid">
      <div class="card"><div class="card-head"><h2>Campaigns</h2><a href="#/campaigns" class="small">All campaigns</a></div>
        ${d.campaigns.length ? `<div class="table-wrap"><table><thead><tr><th>Campaign</th><th class="num">Calls</th><th class="num">Connected</th><th class="num">Positive</th></tr></thead><tbody>
          ${d.campaigns.map((c) => `<tr class="click" data-camp="${c.id}"><td>${esc(c.name)} <span class="tag">${esc(c.kind)}</span></td>
            <td class="num">${c.calls}</td><td class="num">${c.calls ? Math.round(100 * c.connected / c.calls) : 0}%</td><td class="num">${c.positive}</td></tr>`).join('')}
          </tbody></table></div>` : '<div class="empty" style="padding:24px 8px">No campaign calls in this period.</div>'}
      </div>
      <div class="card"><div class="card-head"><h2>Outcomes</h2></div>
        ${d.outcomes.length ? d.outcomes.map((o) => `<div class="hbar"><span>${esc(outcomeLabel(o.outcome))}</span>
            <div><div class="bar" style="width:${(100 * o.n / maxOut).toFixed(1)}%"></div></div><span class="val">${o.n}</span></div>`).join('')
          : '<div class="empty" style="padding:24px 8px">Outcomes appear once calls are marked or analysed.</div>'}
      </div>
    </div>
    <div class="dash-grid">
      <div class="card"><div class="card-head"><h2>Agents</h2></div>
        ${d.agents.length ? `<div class="table-wrap"><table><thead><tr><th>Agent</th><th class="num">Calls</th><th class="num">Connected</th><th class="num">Talk time</th><th class="num">Positive</th><th class="num">Score</th></tr></thead><tbody>
          ${d.agents.map((a) => `<tr><td>${a.type === 'ai' ? `<span class="tag ai">AI</span> ` : ''}${esc(a.name)}</td><td class="num">${a.calls}</td>
            <td class="num">${a.connected}</td><td class="num">${dur(a.talk_seconds)}</td><td class="num">${a.positive}</td>
            <td class="num">${a.avg_score != null ? `<span class="score">${a.avg_score}</span>` : '—'}</td></tr>`).join('')}
          </tbody></table></div>` : '<div class="empty" style="padding:24px 8px">No agent calls in this period.</div>'}
      </div>
      <div class="card"><div class="card-head"><h2>Callbacks due</h2></div>
        ${d.callbacks.length ? d.callbacks.map((c) => `<div class="live-row">${icon('calls')}
            <div style="flex:1;min-width:0"><a href="#/calls/${c.id}">${esc(c.contact_name || c.number)}</a>
              <div class="muted small ellipsis">${esc(c.next_step || '')}</div></div>
            <span class="small">${when(c.callback_at)}</span></div>`).join('')
          : '<div class="empty" style="padding:24px 8px">No callbacks scheduled.</div>'}
      </div>
    </div>`;
  const chart = $('#chart');
  box.querySelectorAll('[data-camp]').forEach((tr) => { tr.onclick = () => { location.hash = '#/campaigns/' + tr.dataset.camp; }; });
  // draw at the card's real width (after layout), and again whenever it changes
  let lastW = 0;
  const redraw = () => { if (chart.clientWidth && chart.clientWidth !== lastW) { lastW = chart.clientWidth; drawChart(chart, d.daily); } };
  requestAnimationFrame(redraw);
  if (window.ResizeObserver) new ResizeObserver(redraw).observe(chart);
  ctx.setRefresh(() => { if (location.hash === '' || location.hash.startsWith('#/dashboard')) viewDashboard(el, ctx); });
}
