// Observability dashboard: metrics, traces, calls and logs from /api/ops/*. Vanilla DOM, no third-party code (CSP: script-src 'self').
const $ = id => document.getElementById(id);
const state = { tab: 'overview', window: '1h', call: '', busy: false };

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...children.flat().filter(c => c != null && c !== false).map(c => (c instanceof Node ? c : String(c))));
  return el;
}
const svg = (tag, attrs = {}, ...children) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...children);
  return el;
};
const ms = v => v == null ? '–' : v >= 1000 ? `${(v / 1000).toFixed(v >= 10_000 ? 0 : 1)} s` : `${Math.round(v)} ms`;
const num = v => v == null ? '–' : Number(v).toLocaleString();
const pct = (a, b) => b ? `${(100 * a / b).toFixed(a && a / b < .01 ? 2 : 1)}%` : '–';
const clock = t => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const short = id => id ? id.slice(0, 8) : '–';
const tag = (text, kind = text) => text == null ? '' : h('span', { class: `tag ${kind}` }, text);

async function get(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}
function fail(e) { $('error').textContent = e ? e.message : ''; $('error').hidden = !e; }
function table(headers, rows, onRow) {
  if (!rows.length) return h('p', { class: 'empty' }, 'No data in this window yet. Make a call on the voice agent to generate telemetry.');
  return h('table', {},
    h('thead', {}, h('tr', {}, headers.map(([label, cls]) => h('th', { class: cls }, label)))),
    h('tbody', {}, rows.map(({ cells, key }) => h('tr', onRow ? { class: 'click', tabindex: 0, onclick: () => onRow(key), onkeydown: e => { if (e.key === 'Enter') onRow(key); } } : {},
      cells.map(([value, cls]) => h('td', { class: cls }, value))))));
}

// ---------- Charts (hand-drawn SVG) ----------
function frame(el, series, max, unit) {
  const W = 600, H = 170, L = 44, B = 20, T = 8, root = svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img' });
  const y = v => T + (H - T - B) * (1 - v / (max || 1)), x = i => L + (W - L - 6) * i / series.length;
  for (const f of [0, .5, 1]) {
    root.append(svg('line', { class: 'axis', x1: L, x2: W - 6, y1: y(max * f), y2: y(max * f) }));
    root.append(svg('text', { x: L - 6, y: y(max * f) + 3, 'text-anchor': 'end' }, unit === 'ms' ? ms(max * f) : num(Math.round(max * f))));
  }
  for (const i of [0, Math.floor(series.length / 2), series.length - 1]) root.append(svg('text', { x: x(i), y: H - 5 }, clock(series[i].t).slice(0, 5)));
  el.replaceChildren(root);
  return { root, x, y, step: (W - L - 6) / series.length };
}
function bars(el, series) {
  const max = Math.max(1, ...series.map(b => b.requests));
  const { root, x, y, step } = frame(el, series, max);
  series.forEach((b, i) => {
    if (!b.requests) return;
    root.append(svg('rect', { class: 'bar-ok', x: x(i) + 1, width: Math.max(1, step - 2), y: y(b.requests), height: y(0) - y(b.requests) }, svg('title', {}, `${clock(b.t)} · ${b.requests} requests, ${b.errors} errors`)));
    if (b.errors) root.append(svg('rect', { class: 'bar-err', x: x(i) + 1, width: Math.max(1, step - 2), y: y(b.errors), height: y(0) - y(b.errors) }));
  });
}
function lines(el, series, keys) {
  const values = series.flatMap(b => keys.map(([k]) => b[k]).filter(v => v != null));
  if (!values.length) { el.replaceChildren(h('p', { class: 'empty' }, 'No samples in this window yet.')); return; }
  const { root, x, y, step } = frame(el, series, Math.max(...values) * 1.1, 'ms');
  for (const [k, cls] of keys) {
    let d = '';
    series.forEach((b, i) => { if (b[k] != null) d += `${d ? 'L' : 'M'}${(x(i) + step / 2).toFixed(1)},${y(b[k]).toFixed(1)}`; });
    root.append(svg('path', { class: cls, d }));
    series.forEach((b, i) => { if (b[k] != null) root.append(svg('circle', { cx: x(i) + step / 2, cy: y(b[k]), r: 2.4, class: cls, fill: 'currentColor', stroke: 'none' }, svg('title', {}, `${clock(b.t)} · ${k} ${ms(b[k])}`))); });
  }
  el.append(h('div', { class: 'legend' }, keys.map(([k, cls, label]) => h('span', {}, h('i', { class: cls }), label))));}

// ---------- Overview ----------
function kpi(label, value, note, mood) { return h('div', { class: `kpi ${mood || ''}` }, h('span', {}, label), h('b', {}, value), note && h('small', {}, note)); }
async function overview() {
  const s = await get(`/api/ops/summary?window=${state.window}`), k = s.kpis;
  const errorRate = k.requests ? k.serverErrors / k.requests : 0;
  $('kpis').replaceChildren(
    kpi('Calls', num(k.calls), `${num(k.activeCalls)} active now`),
    kpi('API requests', num(k.requests), `${num(k.rateLimited)} rate-limited · ${num(k.busy)} shed`),
    kpi('Server error rate', pct(k.serverErrors, k.requests), `${num(k.serverErrors)} errors`, errorRate > .01 ? 'bad' : k.requests ? 'good' : ''),
    kpi('API latency p95', ms(k.p95), `p50 ${ms(k.p50)} · p99 ${ms(k.p99)}`),
    kpi('Voice reply p50', ms(k.voiceP50), `p95 ${ms(k.voiceP95)} · ${num(k.voiceSamples)} turns`),
    kpi('Tool calls', num(k.toolCalls), 'executed by the agent'),
    kpi('Identity verified', `${num(k.verified)}`, `${num(k.verifyAttempts)} code attempts`),
    kpi('Approval judge p95', ms(k.judgeP95), `${num(k.judgeCalls)} judgments`),
    kpi('Handoffs', num(k.handoffs), 'to a representative'),
    kpi('Idle hang-ups', num(k.idleHangups), 'no response from caller'),
    kpi('Reconnects', num(k.reconnects), 'voice session resumed'),
    kpi('Placeholder speech', num(k.placeholders), 'agent spoke markup aloud', k.placeholders ? 'bad' : ''),
    kpi('Silence nudges', num(k.nudges), 'agent prompted to answer'),
  );
  const labels = { voice_token: 'Gemini Live token', policy: 'Reviewed airline policies', consent: 'Approval judges', database: 'Database' };
  $('deps').replaceChildren(...Object.entries(s.dependencies).map(([name, d]) => {
    const mood = d.circuit === 'open' ? 'bad' : d.failures ? 'warn' : d.calls ? 'ok' : 'idle';
    return h('div', { class: 'dep' },
      h('strong', {}, labels[name] || name, h('span', { class: `pill ${mood}` }, d.circuit === 'open' ? 'circuit open' : d.failures ? 'degraded' : d.calls ? 'healthy' : 'idle')),
      h('div', { class: 'muted' }, `${num(d.calls)} calls · ${num(d.failures)} failed · p95 ${ms(d.p95)}`),
      d.lastCircuitEvent && h('div', { class: 'muted' }, `Last circuit event ${clock(d.lastCircuitEvent)}`));
  }));
  bars($('chart-traffic'), s.series);
  lines($('chart-latency'), s.series, [['p50', 'p50', 'p50'], ['p95', 'p95', 'p95']]);
  lines($('chart-voice'), s.series, [['voiceP50', 'voice', 'Voice reply p50']]);

  const a = s.approvals, judged = Object.entries(s.judgeDecisions);
  const top = Math.max(1, ...Object.values(a));
  $('approvals').replaceChildren(h('div', { class: 'funnel' },
    [['Changes approved', a.approved], ['Held for a clearer answer', a.heldForClarity], ['Escalated to keypad', a.reachedKeypad], ['Keypad presses', a.keypadPresses], ['Declined', a.declined]]
      .map(([label, v]) => {
        const fill = h('i');
        fill.style.width = `${100 * v / top}%`;  // CSSOM, not a style attribute: CSP style-src 'self' blocks those.
        return h('div', { class: 'funnel-row' }, h('span', {}, label), h('div', {}, fill), h('b', {}, num(v)));
      })),
    h('h3', {}, 'Judge decisions'),
    judged.length ? h('div', {}, judged.map(([d, n]) => tag(`${d} · ${n}`, d === 'approve' ? 'done' : d === 'unclear' ? 'waiting' : ''))) : h('p', { class: 'muted' }, 'No approvals judged in this window.'));

  $('tools').replaceChildren(table([['Tool'], ['Calls', 'num'], ['Outcomes'], ['p95', 'num']],
    s.tools.map(t => ({ key: t.tool, cells: [[t.tool], [num(t.count), 'num'], [Object.entries(t.outcomes).map(([o, n]) => tag(`${o} ${n}`, o))], [ms(t.p95), 'num']] })),
    tool => { $('trace-q').value = tool; show('traces'); }));
  $('routes').replaceChildren(table([['Route'], ['Requests', 'num'], ['Errors', 'num'], ['p50', 'num'], ['p95', 'num']],
    s.routes.map(r => ({ key: r.route, cells: [[r.route], [num(r.count), 'num'], [r.errors ? h('span', { class: 'lvl-error' }, num(r.errors)) : '0', 'num'], [ms(r.p50), 'num'], [ms(r.p95), 'num']] })),
    route => { $('trace-q').value = route; show('traces'); }));
}

// ---------- Traces ----------
async function traces() {
  const q = new URLSearchParams({ window: state.window, q: $('trace-q').value.trim(), errors: $('trace-errors').checked, call: state.call });
  const { traces } = await get(`/api/ops/traces?${q}`);
  $('trace-call').hidden = !state.call;
  $('trace-call').replaceChildren(`Call ${state.call}`, h('button', { type: 'button', 'aria-label': 'Clear call filter', onclick: () => { state.call = ''; refresh(); } }, '✕'));
  $('trace-list').replaceChildren(table([['Time'], ['Root span'], ['Tool / outcome'], ['Status'], ['Spans', 'num'], ['Duration', 'num'], ['Call'], ['Trace']],
    traces.map(t => ({ key: t.traceId, cells: [[clock(t.start)], [t.name], [[t.tool && tag(t.tool, ''), tag(t.outcome)]], [t.errors ? tag('error') : t.status ? tag(String(t.status), t.status >= 400 ? 'waiting' : 'ok') : tag('ok')],
      [num(t.spans), 'num'], [ms(t.durationMs), 'num'], [h('span', { class: 'mono' }, short(t.call))], [h('span', { class: 'mono' }, short(t.traceId))]] })),
    openTrace));
}
function kindOf(s) {
  if (s.status === 'error') return 'error';
  if (s.service === 'browser') return 'browser';
  if (s.name.startsWith('db.')) return 'db';
  if (s.name.startsWith('consent.')) return 'judge';
  if (s.name.startsWith('workflow.')) return 'workflow';
  return '';
}
async function openTrace(traceId) {
  const { spans, logs } = await get(`/api/ops/trace/${traceId}`);
  if (!spans.length) return;
  const ids = new Set(spans.map(s => s.span_id)), children = new Map();
  for (const s of spans) { const p = ids.has(s.parent_id) ? s.parent_id : null; children.set(p, [...(children.get(p) || []), s]); }
  const ordered = [], walk = (parent, depth) => (children.get(parent) || []).sort((a, b) => a.start_ms - b.start_ms).forEach(s => { ordered.push([s, depth]); walk(s.span_id, depth + 1); });
  walk(null, 0);
  const start = Math.min(...spans.map(s => s.start_ms)), total = Math.max(1, Math.max(...spans.map(s => s.start_ms + s.duration_ms)) - start);
  const root = ordered[0][0];
  $('drawer-title').textContent = root.name;
  $('drawer-meta').textContent = `${traceId} · ${spans.length} spans · ${ms(total)} · ${new Date(start).toLocaleString()}`;
  const detail = s => {
    $('waterfall').querySelectorAll('.span-row').forEach(r => r.classList.toggle('selected', r.dataset.id === s.span_id));
    const attrs = { service: s.service, kind: s.kind, status: s.status, duration: ms(s.duration_ms), 'start offset': ms(s.start_ms - start), call: s.call, ...s.attrs };
    $('span-detail').replaceChildren(h('h3', {}, s.name), h('dl', {}, Object.entries(attrs).filter(([, v]) => v != null && v !== '').flatMap(([k, v]) => [h('dt', {}, k), h('dd', { class: 'mono' }, String(v))])));
  };
  $('waterfall').replaceChildren(...ordered.map(([s, depth]) => {
    const bar = h('i', { class: kindOf(s), title: `${s.name} · ${ms(s.duration_ms)}` });
    bar.style.left = `${100 * (s.start_ms - start) / total}%`; bar.style.width = `${Math.max(.3, 100 * s.duration_ms / total)}%`;
    const name = h('span', { class: 'span-name', title: s.name }, s.name);
    name.style.paddingLeft = `${depth * 12}px`;
    return h('button', { type: 'button', class: 'span-row', 'data-id': s.span_id, onclick: () => detail(s) }, name, h('span', { class: 'span-track' }, bar), h('span', { class: 'span-dur' }, ms(s.duration_ms)));
  }));
  detail(root);
  $('trace-logs').replaceChildren(table([['Time'], ['Level'], ['Event'], ['Message']],
    logs.map(l => ({ cells: [[clock(l.ts)], [h('span', { class: `lvl-${l.level}` }, l.level)], [l.event], [h('span', { class: 'mono' }, l.message)]] }))));
  $('drawer').hidden = false; $('drawer-close').focus();
}

// ---------- Calls (one row per AI session) ----------
const words = v => String(v ?? '').replaceAll('_', ' ');
const tokens = t => t.input || t.output ? `${num(t.input + t.output)}` : '–';
async function calls() {
  const { calls } = await get(`/api/ops/calls?window=${state.window}`);
  $('call-list').replaceChildren(table([['Started'], ['Call'], ['Duration', 'num'], ['Turns', 'num'], ['Tools', 'num'], ['Changes', 'num'], ['Verified'],
    ['Voice p50', 'num'], ['Interrupts', 'num'], ['Recoveries', 'num'], ['Tokens', 'num'], ['Errors', 'num'], ['Ended']],
    calls.map(c => ({ key: c.call, cells: [[clock(c.start)], [h('span', { class: 'mono' }, c.call)], [ms(c.durationMs), 'num'], [num(c.turns), 'num'], [num(c.tools), 'num'],
      [num(c.changes), 'num'], [c.verified ? tag('yes', 'ok') : tag('no', '')], [ms(c.voiceP50), 'num'], [num(c.interruptions), 'num'],
      [num(c.nudges + c.idleCheckins + c.reconnects + c.refusalsHeld), 'num'], [tokens({ input: c.tokens.live.input + c.tokens.judges.input, output: c.tokens.live.output + c.tokens.judges.output }), 'num'],
      [c.errors ? h('span', { class: 'lvl-error' }, num(c.errors)) : '0', 'num'],
      [tag(words(c.ended), c.ended === 'active' ? 'ok' : c.ended === 'no_response' ? 'waiting' : '')]] })),
    openCall));
}

// What each span means for the call, in plain words: [category, title, detail].
function describe(e) {
  const a = e.attrs, judges = e.judges?.length ? `judges: ${e.judges.map(j => `${j.role} ${j.decision}${j.rejected ? ` (${j.rejected})` : ''} ${ms(j.durationMs)}`).join(', ')}` : '';
  const assessed = e.assessment ? `approval judged ${e.assessment.decision}${e.assessment.mode ? ` (${e.assessment.mode} step)` : ''}` : '';
  const deps = (e.dependencies || []).map(d => `${d.name} ${ms(d.durationMs)}${d.status === 'error' ? ' failed' : ''}`).join(', ');
  const join = (...parts) => parts.filter(Boolean).join(' · ');
  switch (e.name) {
    case 'browser.call_setup': return ['call', 'Call connected', a.microphone === false ? 'No microphone: caller is typing' : `Connected in ${ms(e.durationMs)}`];
    case 'browser.call_end': return ['call', `Call ended: ${words(a.reason)}`, join(`${num(a.turns)} agent turns`, a.inputTokens != null && `${num(a.inputTokens)} in / ${num(a.outputTokens)} out voice-model tokens`)];
    case 'browser.agent_turn': if (a.placeholder) return ['agent', 'Agent spoke a placeholder (such as "<no speech>")', `${num(a.inputTokens)} in / ${num(a.outputTokens)} out tokens`];
      return ['agent', a.interrupted ? 'Agent turn (cut off by caller)' : a.spoke === false ? 'Agent responded without speaking' : 'Agent spoke', `${num(a.inputTokens)} in / ${num(a.outputTokens)} out tokens`];
    case 'browser.voice_latency': return ['latency', 'Agent started replying', `${ms(e.durationMs)} after the caller finished`];
    case 'browser.interrupted': return ['caller', 'Caller interrupted the agent', `after ${ms(a.agentSpokeMs)} of agent speech`];
    case 'browser.tool_cancelled': return ['caller', `Voice model cancelled ${a.count} tool call${a.count === 1 ? '' : 's'}`, 'Caller spoke before they finished'];
    case 'browser.reply_nudge': return ['recovery', 'Silent agent nudged to answer', `${ms(e.durationMs)} of silence after ${a.after === 'quote_reply' ? "the caller's reply to a quote" : 'a tool result'}`];
    case 'browser.idle_checkin': return ['recovery', 'Agent asked if the caller is still there', `${ms(e.durationMs)} of silence`];
    case 'browser.idle_goodbye': return ['recovery', 'Agent said goodbye to a silent caller', `${ms(e.durationMs)} of silence`];
    case 'browser.refusal_held': return ['recovery', a.outcome === 'spoken' ? 'Early tool call: agent waited, then answered gently' : 'Early tool call: refusal dropped because the caller spoke', words(a.tool)];
    case 'browser.reconnect': return ['recovery', 'Voice session reconnected', join(words(a.reason), `attempt ${a.attempt}`, a.resumed && 'conversation resumed')];
    case 'browser.keypad': return ['caller', `Keypad: pressed ${a.key === 'confirm' ? '1 to confirm' : '7 to cancel'}`, ''];
  }
  const route = a['http.route'], status = a['http.status_code'];
  const failed = status >= 400 ? `HTTP ${status}` : '';
  if (route === '/api/turn') return ['caller', a.channel === 'keypad' ? 'Key press recorded' : 'Caller turn recorded',
    join(a.stage && `stage: ${words(a.stage)}`, a.verification && `code ${a.verification}`, a.declined && 'caller declined', assessed, judges, failed)];
  if (route === '/api/tool') return ['tool', `Tool: ${words(a.tool)}`,
    join(a.outcome && `outcome: ${a.outcome}`, a.action && words(a.action), a.approvalStep && `approval step: ${a.approvalStep}`, a.stage && `stage: ${words(a.stage)}`, assessed, judges, deps, failed)];
  if (route === '/api/token') return ['call', a.reconnect ? 'Voice token renewed' : 'Voice session token issued', join(a.outcome && words(a.outcome), deps, failed)];
  if (route === '/api/end') return ['call', 'Call closed on the server', failed];
  if (route === '/api/cancel') return ['caller', 'Cancelled tool calls recorded', `${num(a.cancelled)} cancelled`];
  return ['other', e.name, failed];
}
async function openCall(call) {
  const { summary: c, events, approvals } = await get(`/api/ops/call/${call}`);
  state.openCall = call;
  $('call-title').textContent = `Call ${call}`;
  $('call-meta').textContent = `${new Date(c.start).toLocaleString()} · ${ms(c.durationMs)} · ended: ${words(c.ended)}`;
  const t = c.tokens;
  $('call-kpis').replaceChildren(
    kpi('Verified', c.verified ? 'Yes' : 'No', null, c.verified ? 'good' : ''),
    kpi('Caller turns', num(c.turns), `${num(c.agentTurns)} agent turns`),
    kpi('Tool calls', num(c.tools), `${num(c.toolsCancelled)} cancelled`),
    kpi('Changes made', num(c.changes), c.handoff ? 'handed to a representative' : null),
    kpi('Voice reply p50', ms(c.voiceP50), `p95 ${ms(c.voiceP95)}`),
    kpi('Interruptions', num(c.interruptions), 'caller spoke over agent'),
    kpi('Recoveries', num(c.nudges + c.idleCheckins + c.reconnects + c.refusalsHeld),
      `${num(c.nudges)} nudges · ${num(c.idleCheckins)} check-ins · ${num(c.reconnects)} reconnects · ${num(c.refusalsHeld)} held`),
    kpi('Voice model tokens', tokens(t.live), `${num(t.live.input)} in · ${num(t.live.output)} out`),
    kpi('Judge tokens', tokens(t.judges), `${num(t.judges.calls)} judgments`),
    kpi('Placeholder speech', num(c.placeholders), 'spoken markup like "<no speech>"', c.placeholders ? 'bad' : ''),
    kpi('Server errors', num(c.errors), null, c.errors ? 'bad' : ''),
  );
  $('call-stages').replaceChildren(...(c.stages.length ? c.stages.flatMap((stage, i) => [i ? h('span', { class: 'arrow' }, '→') : null, tag(words(stage), '')]).filter(Boolean)
    : [h('span', { class: 'muted' }, 'No workflow stages recorded.')]));
  $('call-approvals').replaceChildren(...(approvals.length ? approvals.map(q => h('div', { class: 'quote' },
    h('div', { class: 'quote-head' }, h('span', { class: 'mono' }, `Quote ${q.proposal}`),
      h('span', {}, q.reachedKeypad && tag('reached keypad', 'waiting'), tag(q.result, q.result === 'committed' ? 'done' : q.result === 'pending' ? 'waiting' : ''))),
    h('ol', {}, q.steps.map(step => h('li', {}, step.step, h('small', {}, clock(step.t))))))) : [h('p', { class: 'muted' }, 'No changes were quoted on this call.')]));
  $('call-timeline').replaceChildren(...events.map(e => {
    const [category, title, detail] = describe(e), failed = e.status === 'error' || e.attrs['http.status_code'] >= 500;
    const offset = Math.max(0, Math.round((e.t - c.start) / 1000));
    const parts = [h('span', { class: 'at' }, `+${String(Math.floor(offset / 60)).padStart(2, '0')}:${String(offset % 60).padStart(2, '0')}`),
      h('span', { class: `dot ${failed ? 'error' : category}`, 'aria-hidden': 'true' }), h('div', {}, h('strong', {}, title), detail && h('p', {}, detail)),
      h('span', { class: 'dur' }, e.durationMs >= 1 ? ms(e.durationMs) : '')];
    return e.service === 'browser' ? h('div', { class: `event${failed ? ' failed' : ''}` }, parts)
      : h('button', { type: 'button', class: `event${failed ? ' failed' : ''}`, title: 'Open trace', onclick: () => openTrace(e.traceId).catch(fail) }, parts);
  }));
  $('call-drawer').hidden = false; $('call-close').focus();
}

// ---------- Logs ----------
async function logs() {
  const q = new URLSearchParams({ window: state.window, level: $('log-level').value, q: $('log-q').value.trim() });
  const { logs } = await get(`/api/ops/logs?${q}`);
  $('log-list').replaceChildren(table([['Time'], ['Level'], ['Event'], ['Message'], ['Call'], ['Trace']],
    logs.map(l => ({ key: l.trace_id, cells: [[clock(l.ts)], [h('span', { class: `lvl-${l.level}` }, l.level)], [l.event], [h('span', { class: 'mono' }, l.message)], [h('span', { class: 'mono' }, short(l.call))], [l.trace_id ? h('span', { class: 'mono' }, short(l.trace_id)) : '–']] })),
    traceId => { if (traceId) openTrace(traceId).catch(fail); }));
}

// ---------- Shell ----------
const views = { overview, traces, calls, logs };
async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try { await views[state.tab](); fail(null); $('updated').textContent = `Updated ${clock(Date.now())}`; }
  catch (e) { fail(e); }
  finally { state.busy = false; }
}
function show(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  for (const name of Object.keys(views)) $(name).hidden = name !== tab;
  history.replaceState(null, '', `#${tab}`);
  void refresh();
}
document.querySelectorAll('.tabs button').forEach(b => { b.onclick = () => show(b.dataset.tab); });
$('window').onchange = e => { state.window = e.target.value; void refresh(); };
$('drawer-close').onclick = () => { $('drawer').hidden = true; };
$('call-close').onclick = () => { $('call-drawer').hidden = true; };
$('call-traces').onclick = () => { state.call = state.openCall; $('call-drawer').hidden = true; $('trace-q').value = ''; show('traces'); };
// Escape closes the top drawer: a trace opened from a call returns to that call.
document.addEventListener('keydown', e => { if (e.key === 'Escape') { if (!$('drawer').hidden) $('drawer').hidden = true; else $('call-drawer').hidden = true; } });
let typing;
for (const id of ['trace-q', 'log-q']) $(id).oninput = () => { clearTimeout(typing); typing = setTimeout(refresh, 300); };
for (const id of ['trace-errors', 'log-level']) $(id).onchange = () => void refresh();
for (const id of ['trace-filter', 'log-filter']) $(id).onsubmit = e => { e.preventDefault(); void refresh(); };
setInterval(() => { if ($('auto').checked && !document.hidden && $('drawer').hidden && $('call-drawer').hidden) void refresh(); }, 10_000);
window.addEventListener('hashchange', () => { const tab = location.hash.slice(1); if (views[tab] && tab !== state.tab) show(tab); });
show(views[location.hash.slice(1)] ? location.hash.slice(1) : 'overview');
