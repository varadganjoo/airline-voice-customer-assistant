// Checks the real browser request/turn helpers without a browser or live provider.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

// Text-only calls must never open the device, including before the user can press Mute.
const frontendSource = await readFile('frontend/app.js', 'utf8');
let microphoneRequests = 0;
const microphoneOnly = frontendSource.slice(frontendSource.indexOf('async function microphone()'), frontendSource.indexOf('function flushTools()'));
const disabledMicrophone = vm.runInNewContext(microphoneOnly + '\nmicrophone;', {
  URLSearchParams, DOMException, location: { search: '?microphone=off' },
  navigator: { mediaDevices: { getUserMedia: async () => { microphoneRequests++; throw new Error('Device opened'); } } },
});
await assert.rejects(disabledMicrophone(), error => error.name === 'NotAllowedError');
assert.equal(microphoneRequests, 0);
console.log('PASS: microphone=off never requests or opens an audio input device');

const app = (await readFile('frontend/app.js', 'utf8')).replace(/^import[^\n]+\n/, '');
const source = app.split('function text(parent')[0] + app.slice(app.indexOf('function flushTools()'), app.indexOf('function showSource('));
let requests = [], fetchImpl, rendered = [], sent = [];
const context = vm.createContext({ crypto: webcrypto, AbortSignal, TypeError, document: { getElementById: () => ({}) }, transcript() {}, renderCase: state => rendered.push(state), showSource() {}, reconnect() { throw new Error('Unexpected reconnect'); }, fetch: (...args) => fetchImpl(...args), setTimeout: fn => { queueMicrotask(fn); return 1; }, clearTimeout() {} });
vm.runInContext(source + '\nthis.helpers = { api, retryApi, toolRequest, beginCallerTurn, finishCallerTurn, recordInputTranscription, executeTool, toolResponse };', context);
const { api, retryApi, toolRequest, beginCallerTurn, finishCallerTurn, recordInputTranscription } = context.helpers;
const response = (status, data) => ({ ok: status < 400, status, json: async () => data });
fetchImpl = async (route, options) => { requests.push({ route, body: JSON.parse(options.body) }); return response(200, {}); };
vm.runInContext("callId = 'call'; selectedProposal = { id: 'proposal' };", context);
const turn = beginCallerTurn('yes, go ahead');
finishCallerTurn(); finishCallerTurn();
await turn.registered;
assert.equal(requests.length, 1);
assert.equal(requests[0].body.proposalId, 'proposal');
assert.equal(requests[0].body.turnId, turn.id);

// The judged turn carries what the agent said, and a commit re-sends exactly that turn.
requests = [];
vm.runInContext("agentSince = 'That is fifty-nine dollars. Shall I go ahead?';", context);
const quoted = beginCallerTurn('Yeah go for it'); finishCallerTurn(); await quoted.registered;
assert.equal(requests[0].body.agentText, 'That is fifty-nine dollars. Shall I go ahead?');
assert.equal(requests[0].body.channel, 'voice');
assert.equal(vm.runInContext('agentSince', context), '');
fetchImpl = async (route, options) => { requests.push({ route, body: JSON.parse(options.body) }); return response(200, { result: { done: true } }); };
vm.runInContext("running = ready = true; live = { sendToolResponse() {} };", context);
await context.helpers.executeTool({ id: 'commit-1', name: 'confirm_action', args: { proposalId: 'proposal' } }, 'call');
const commit = requests.find(r => r.route.endsWith('/tool'));
assert.equal(commit.body.turnText, 'Yeah go for it');
assert.equal(commit.body.agentText, 'That is fifty-nine dollars. Shall I go ahead?');
vm.runInContext("running = ready = false; live = undefined;", context);
fetchImpl = async (route, options) => { requests.push({ route, body: JSON.parse(options.body) }); return response(200, {}); };

requests = [];
recordInputTranscription({ interimInputTranscription: { text: 'yes' } });
assert.equal(requests.length, 0);
recordInputTranscription({ inputTranscription: { text: 'Yes, but wait.' } }); // Actual provider omits finished.
assert.equal(requests.length, 0);
finishCallerTurn();
await vm.runInContext('callerTurn.registered', context);
assert.equal(requests.length, 1);
assert.equal(requests[0].body.text, 'Yes, but wait.');
recordInputTranscription({ inputTranscription: { text: 'Yes', finished: false } });
assert.equal(requests.length, 1);
recordInputTranscription({ inputTranscription: { text: ', go ahead', finished: true } });
await vm.runInContext('callerTurn.registered', context);
assert.equal(requests.at(-1).body.text, 'Yes, go ahead');
recordInputTranscription({ inputTranscription: { text: 'Three Quebec ' } });
recordInputTranscription({ inputTranscription: { text: 'five Yankee Yankee Uniform' } });
finishCallerTurn();
await vm.runInContext('callerTurn.registered', context);
assert.equal(requests.at(-1).body.text, 'Three Quebec five Yankee Yankee Uniform');

fetchImpl = async () => response(200, { state: { proposals: [] } });
const withdrawal = beginCallerTurn('wait'); finishCallerTurn(); await withdrawal.registered;
assert.equal(rendered.length, 1); // Turn response clears withdrawn proposal immediately.
let resolveOld;
fetchImpl = () => new Promise(resolve => { resolveOld = resolve; });
const older = beginCallerTurn('thanks'); finishCallerTurn(); await Promise.resolve();
beginCallerTurn('a new question');
resolveOld(response(200, { state: { proposals: ['stale'] } })); await older.registered;
assert.equal(rendered.length, 1); // Late response cannot overwrite the newer turn.

context.liveMock = { sendToolResponse: data => sent.push(data.functionResponses[0]) };
vm.runInContext("running = ready = true; live = liveMock; callerTurn = undefined;", context);
let finishPolicy;
fetchImpl = () => new Promise(resolve => { finishPolicy = resolve; });
const fc = { id: 'async-policy', name: 'lookup_policy', args: { topic: 'refunds' } };
const pending = context.helpers.executeTool(fc, 'call');
await context.helpers.executeTool(fc, 'call'); // Duplicate provider ID must not start work again.
assert.equal(sent.length, 1);
assert.equal(sent[0].willContinue, true);
assert.equal(sent[0].response.result.status, 'in_progress');
assert.equal(sent[0].scheduling, 'SILENT'); // Progress must not trigger a second acknowledgment.
finishPolicy(response(200, { result: { facts: ['Policy found'] } })); await pending;
assert.equal(sent.length, 2);
assert.equal(sent[1].willContinue, false);
assert.equal(sent[1].scheduling, 'WHEN_IDLE');
assert.equal(sent[1].response.result.facts[0], 'Policy found');
assert.equal(context.helpers.toolResponse({ id: 'notes', name: 'save_notes' }, { saved: true }).scheduling, 'SILENT');

// A slow tool gets one acknowledgement reminder only if the agent has stayed silent.
const originalTimeout = context.setTimeout, originalClearTimeout = context.clearTimeout;
const timers = new Map(); let timerId = 0;
context.setTimeout = (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; };
context.clearTimeout = id => timers.delete(id);
for (const scenario of ['silent', 'spoken', 'speaking', 'new-turn', 'cancelled', 'disconnected', 'fast', 'notes', 'handoff']) {
  sent = []; const resolvers = [];
  fetchImpl = async () => response(200, {});
  const ackTurn = beginCallerTurn('What is the refund policy?'); finishCallerTurn(); await ackTurn.registered;
  vm.runInContext("agentSince = ''; modelGenerating = false; ready = true;", context);
  fetchImpl = () => new Promise(resolve => resolvers.push(resolve));
  const name = scenario === 'notes' ? 'save_notes' : scenario === 'handoff' ? 'transfer_to_representative' : 'lookup_policy';
  const firstId = `ack-${scenario}`;
  const jobs = [context.helpers.executeTool({ ...fc, id: firstId, name }, 'call')];
  if (scenario === 'silent') jobs.push(context.helpers.executeTool({ ...fc, id: `${firstId}-second` }, 'call'));
  // Let saved-turn registration settle while both tool requests remain unresolved.
  for (let i = 0; i < 8; i++) await Promise.resolve();
  if (scenario === 'spoken') vm.runInContext("agentSince = 'Let me check that for you.';", context);
  if (scenario === 'speaking') vm.runInContext('modelGenerating = true;', context);
  if (scenario === 'new-turn') beginCallerTurn('Actually, never mind.');
  if (scenario === 'cancelled') vm.runInContext(`cancelled.add('${firstId}');`, context);
  if (scenario === 'disconnected') vm.runInContext('ready = false;', context);
  if (scenario === 'fast') { resolvers.forEach(resolve => resolve(response(200, { result: {} }))); await Promise.all(jobs); }
  for (const { fn, ms } of [...timers.values()]) if (ms === 800) fn();
  const reminders = sent.filter(r => r.willContinue && r.scheduling === 'WHEN_IDLE');
  assert.equal(reminders.length, scenario === 'silent' ? 1 : 0, scenario);
  if (reminders.length) {
    assert.match(reminders[0].response.result.instruction, /exactly one short sentence/i);
    // Never let a speech-only model choose silence: it speaks a placeholder such as "<no speech>" instead.
    assert.doesNotMatch(reminders[0].response.result.instruction, /stay silent|if you have not|or nothing/i);
    assert.match(reminders[0].response.result.instruction, /current conversational language/i);
    assert.equal(reminders[0].response.result.callerRequest, ackTurn.text);
  }
  resolvers.forEach(resolve => resolve(response(200, { result: {} }))); await Promise.all(jobs);
  assert.equal([...timers.values()].filter(t => t.ms === 800).length, 0);
  timers.clear();
}
context.setTimeout = originalTimeout; context.clearTimeout = originalClearTimeout;
vm.runInContext("callerTurn = undefined; agentSince = ''; modelGenerating = false; ready = true;", context);
fetchImpl = () => new Promise(resolve => { finishPolicy = resolve; });

sent = [];
const cancelledCall = context.helpers.executeTool({ ...fc, id: 'cancelled-policy' }, 'call');
vm.runInContext("cancelled.add('cancelled-policy');", context);
finishPolicy(response(200, { result: { facts: ['Late result'] } })); await cancelledCall;
assert.equal(sent.length, 1); // Progress was sent, but cancelled completion is suppressed.

let finishTransfer, shownBrief, completedTransfers = 0;
sent = [];
context.renderHandoff = brief => { shownBrief = brief; };
context.handoff = async (message, response) => {
  assert.equal(message, 'Forwarding.');
  assert.equal(response, undefined, 'a cancelled provider call uses the local announcement fallback');
  completedTransfers++;
};
fetchImpl = () => new Promise(resolve => { finishTransfer = resolve; });
const transferJob = context.helpers.executeTool({ id: 'transfer', name: 'transfer_to_representative', args: { reason: 'Asked for a person.', notes: 'Needs help.' } }, 'call');
assert.equal(sent.length, 0); // Blocking handoff never sends async progress or releases the model early.
assert.equal(completedTransfers, 0); // No transfer announcement before the saved result.
vm.runInContext("cancelled.add('transfer');", context);
finishTransfer(response(200, { result: { handoff: true, message: 'Forwarding.', brief: { notes: 'Needs help.' } } }));
await transferJob;
assert.equal(shownBrief.notes, 'Needs help.'); // A committed handoff survives a late cancellation.
assert.equal(completedTransfers, 1);
fetchImpl = async () => response(200, { result: { error: 'Handoff storage unavailable.' } });
await context.helpers.executeTool({ id: 'failed-transfer', name: 'transfer_to_representative', args: { reason: 'Asked for a person.', notes: 'Needs help.' } }, 'call');
assert.equal(completedTransfers, 1);
assert.equal(sent.length, 1);
assert.equal('scheduling' in sent[0], false);
assert.equal('willContinue' in sent[0], false);
assert.match(sent[0].response.result.instruction, /transfer has not been confirmed/i);

// Page refreshes must never turn a committed receipt (including a recovered one) into a tool failure.
{
  const originalError = context.error, originalRenderCase = context.renderCase;
  context.renderCaller = () => {};
  context.textToast = () => {};
  for (const scenario of ['trip-network', 'trip-http', 'trip-json', 'trip-render', 'state-render', 'inbox-network', 'recovered', 'success', 'tool-failure']) {
    const warnings = [], routes = [];
    const verification = scenario === 'inbox-network';
    const result = verification ? { sent: true, sentTo: 'Mobile on file' }
      : scenario === 'tool-failure' ? { error: 'Change rejected.' }
      : { done: true, receipt: { id: 'saved-receipt', summary: 'Seat changed to 27C.' } };
    context.error = message => warnings.push(message);
    context.renderCase = () => { if (scenario === 'state-render') throw new Error('Rendering failed'); };
    context.renderCaller = () => { if (scenario === 'trip-render') throw new Error('Rendering failed'); };
    context.refreshInbox = async () => { throw new TypeError('Failed to fetch'); };
    sent = [];
    vm.runInContext('running = ready = true; callerTurn = undefined; live = liveMock;', context);
    fetchImpl = async route => {
      routes.push(route);
      if (route.endsWith('/tool') && scenario === 'recovered') throw new TypeError('Lost commit response');
      if (route.endsWith('/me')) {
        if (['trip-network', 'recovered'].includes(scenario)) throw new TypeError('Failed to fetch');
        if (scenario === 'trip-http') return response(503, { error: 'Unavailable' });
        if (scenario === 'trip-json') return { ok: true, json: async () => { throw new SyntaxError('Invalid JSON'); } };
        return response(200, { caller: {} });
      }
      return response(200, { status: 'completed', result, state: {} });
    };
    await context.helpers.executeTool({ id: `refresh-${scenario}`, name: verification ? 'request_verification' : 'confirm_action', args: {} }, 'call');
    assert.deepEqual(sent.at(-1).response.result, result, `${scenario}: the model gets the original service result`);
    assert.equal(routes.filter(route => route.endsWith('/tool')).length, 1, 'a refresh failure never repeats the operation');
    assert.equal(warnings.length, ['success', 'tool-failure'].includes(scenario) ? 0 : 1, scenario);
    if (warnings.length) assert.match(warnings[0], /page.*refresh/i);
  }
  context.error = originalError; context.renderCase = originalRenderCase;
  console.log('PASS: refresh failures preserve committed/recovered receipts and code-send results without retrying the operation');
}

requests = [];
const body = { callId: 'call', requestId: 'same-id', name: 'confirm_action', args: { proposalId: 'proposal' } };
fetchImpl = async (route, options) => {
  requests.push({ route, body: JSON.parse(options.body) });
  if (route.endsWith('/tool')) throw new TypeError('Connection lost after commit');
  return response(200, { status: 'completed', result: { done: true } });
};
assert.equal((await toolRequest(body)).result.done, true);
assert.equal(requests.filter(r => r.route.endsWith('/tool')).length, 1);
assert.equal(requests[1].body.requestId, body.requestId);

requests = [];
fetchImpl = async (route, options) => {
  requests.push({ route, body: JSON.parse(options.body) });
  return route.endsWith('/tool') ? { ok: false, status: 502, json: async () => { throw new SyntaxError('HTML gateway error'); } } : response(200, { status: 'unknown' });
};
const unresolved = await toolRequest(body);
assert.equal(unresolved.result.outcomeUnknown, true);
assert.equal(requests.filter(r => r.route.endsWith('/tool')).length, 3);
assert.ok(requests.every(r => r.body.requestId === 'same-id'));

requests = [];
fetchImpl = async (route, options) => { requests.push({ route, body: JSON.parse(options.body) }); return response(403, { error: 'Private demo' }); };
await assert.rejects(() => retryApi('turn', { turnId: 'same-turn' }), /Private demo/);
assert.equal(requests.length, 1);
await assert.rejects(() => api('tool', body), /Private demo/);
requests = [];
fetchImpl = async (route, options) => { requests.push({ route, body: JSON.parse(options.body) }); return response(429, { error: 'Too many requests.', retryable: false }); };
await assert.rejects(() => retryApi('turn', { turnId: 'limited-turn' }), /Too many requests/);
assert.equal(requests.length, 1);

const elements = new Map();
const element = () => ({ textContent: '', hidden: true, dataset: {}, scrollHeight: 100, scrollTop: 0, clientHeight: 100, children: [], append(child) { this.children.push(child); }, replaceChildren() { this.children = []; } });
const view = vm.createContext({ document: { createElement: element }, $: id => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
} });
vm.runInContext(app.slice(app.indexOf('function text(parent'), app.indexOf('function renderCaller(')) + '\nthis.showBrief = renderHandoff; this.showNotes = renderNotes;', view);
view.showBrief({ bookingVerified: true, bookingReference: 'ABC123', reason: 'Caller asked for a person.',
  notes: 'Needs a flight change.', completedActions: ['Seat changed.'], unapprovedProposal: 'Refund quoted.' });
assert.equal(elements.get('handoff-card').hidden, false);
assert.equal(elements.get('handoff-booking').textContent, 'Verified booking ABC123');
assert.equal(elements.get('handoff-notes').textContent, 'Needs a flight change.');
assert.equal(elements.get('handoff-actions').children[0].textContent, 'Seat changed.');
assert.match(elements.get('handoff-pending').textContent, /Not approved or applied/);
console.log('PASS: speech/turn binding, async tools, handoff brief display, durable replay and bounded retries');

view.showNotes([{ id: 'note', timestamp: 1000, kind: 'agent', text: '<script>customer request</script>' }]);
assert.equal(elements.get('notes-count').textContent, '1');
assert.equal(elements.get('interaction-notes').children[0].children[1].textContent, '<script>customer request</script>');
assert.equal(elements.get('interaction-notes').children[0].children[0].children[1].dateTime, '1970-01-01T00:00:01.000Z');
view.showNotes([]); // A late response cannot erase newer saved notes.
assert.equal(elements.get('notes-count').textContent, '1');
console.log('PASS: timestamped notes render as text and survive out-of-order responses');

context.refreshInbox = async () => [{code:'654321'}]; context.textToast = () => {};
const identityUpdates = [];
context.liveMock.sendClientContent = value => identityUpdates.push(value);
vm.runInContext("running = ready = true; live = liveMock; callId = 'call'; callerTurn = undefined; turnQueue = Promise.resolve(); selectedProposal = null;", context);
requests = [];
fetchImpl = async (route, options) => {
 const body=JSON.parse(options.body); requests.push({route,body});
 return response(200, {result:{verificationResult:{sent:true}}});
};
beginCallerTurn("Yes, that's correct."); finishCallerTurn();
await vm.runInContext('callerTurn.registered',context);
assert.equal(requests.length,1);
assert.equal(identityUpdates.length,1);
assert.ok(JSON.stringify(identityUpdates[0]).includes('sent'));
console.log('PASS: a fresh yes continues the pending identity request without waiting for another model tool call');

// A provider outage must not create an endless reconnect storm.
let scheduled = [], stopped = 0, spoken = [];
const recovery = vm.createContext({
  running: true, retryTimer: null, retries: 0, ready: true, pendingHandoff: null,
  $: () => ({}), status() {}, stopPlayback() {},
  setTimeout: (fn, ms) => { scheduled.push(ms); return 1; },
  connect() {}, stop: async () => { stopped++; }, transcript() {}, error() {},
  SpeechSynthesisUtterance: function(text) { this.text = text; },
  speechSynthesis: { speak: utterance => spoken.push(utterance.text) }, Math
});
vm.runInContext(app.slice(app.indexOf('function reconnect()'), app.indexOf('async function checkpoint()')), recovery);
for (let i = 0; i < 5; i++) { vm.runInContext('retryTimer = null; reconnect()', recovery); }
assert.equal(scheduled.length, 5);
vm.runInContext('retryTimer = null; reconnect()', recovery);
await new Promise(resolve => setImmediate(resolve));
assert.equal(stopped, 1);
assert.equal(scheduled.length, 5);
assert.match(spoken[0], /temporarily unavailable/);
assert.ok(scheduled.every((ms, i) => ms >= 1000 * 2 ** i && ms < 1000 * 2 ** i + 1000));
console.log('PASS: reconnects stop after five attempts with jitter and an audible fallback');

// Completed client content interrupts Live generation: defer notices until idle.
const noticeTimers = [];
context.setTimeout = fn => { noticeTimers.push(fn); return noticeTimers.length; };
identityUpdates.length = 0;
vm.runInContext("modelGenerating = true; outputContext = {currentTime:0}; nextPlay = 1;", context);
await vm.runInContext("reportVerification({sent:false}, callerTurn, 'call')", context);
assert.equal(identityUpdates.length, 0);
vm.runInContext('modelGenerating = false', context);
noticeTimers.shift()();
assert.equal(identityUpdates.length, 0); // Audio still playing.
vm.runInContext('outputContext.currentTime = 2', context);
noticeTimers.shift()();
assert.equal(identityUpdates.length, 1);
assert.equal(identityUpdates[0].turnComplete, true);
console.log('PASS: verification notices do not interrupt generation or queued speech');

// Dial pad: only "1" at the keypad step of the approval ladder confirms; other keys are just tones.
{
  const pad = app.slice(app.indexOf('const DTMF'), app.indexOf('// Last step of the approval ladder'));
  const nodes = {}, el = id => (nodes[id] ??= { hidden: true, textContent: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    classList: { on: false, toggle(_, v) { this.on = v; } }, querySelector: q => el(q) });
  const confirms = [];
  const ctx = vm.createContext({ $: el, running: true, outputContext: null, selectedProposal: null, keypadPress: async key => { confirms.push(key); } });
  vm.runInContext(pad + '\nthis.press = key => $("dialpad").querySelector(".dialpad-keys").onclick({ target: { closest: () => ({ dataset: { key } }) } }); this.prompt = promptKeypad;', ctx);
  ctx.press('1');
  ctx.selectedProposal = { approval: { mode: 'closed' } }; ctx.press('1');
  ctx.press('7');
  ctx.selectedProposal = { approval: { mode: 'manual' } }; ctx.press('2'); ctx.press('5'); ctx.press('#');
  assert.equal(confirms.length, 0);
  ctx.press('1'); ctx.press('7');
  assert.deepEqual(confirms, ['1', '7']);
  assert.equal(el('dialpad-display').textContent, '11725#17');
  ctx.prompt(true);
  assert.equal(el('[data-key="1"]').classList.on, true);
  assert.equal(el('dialpad').hidden, false, 'the keypad step opens the pad');
  console.log('PASS: dial pad: 1 confirms and 7 cancels, only at the keypad step');
}

// Silent caller: one check-in after a minute, a goodbye 40 s later, then hang up. Speech or busy agent resets/pauses it.
{
  const idle = app.slice(app.indexOf('function callerActive()'), app.indexOf('function text(parent'));
  const said = [], ended = [];
  let now = 0;
  const ctx = vm.createContext({ Date: { now: () => now }, ready: true, running: true, modelGenerating: false, inputTurnOpen: false, pendingHandoff: null,
    toolsInFlight: 0, outputContext: null, nextPlay: 0, lastCallerActivity: 0, lastAgentActivity: 0, idleStage: 0, goodbyeSentAt: 0,
    IDLE_CHECK_MS: 60_000, IDLE_END_MS: 40_000, live: { sendClientContent: c => said.push(c.turns[0].parts[0].text) },
    stop: async () => {}, transcript: () => {}, status: () => {}, activeTranscript: {}, $: () => ({}), recordSpan: () => {} });
  vm.runInContext(idle.replace('async function endCall', 'async function realEndCall') + '\nthis.check = checkIdle; this.active = callerActive; endCall = async m => ended.push(m);', Object.assign(ctx, { ended }));
  const at = s => { now = s * 1000; ctx.check(); };
  at(59); assert.equal(said.length, 0);
  at(60); assert.match(said[0], /still there/);
  ctx.modelGenerating = true; at(65); ctx.modelGenerating = false;  // The check-in itself is not silence.
  at(104); assert.equal(said.length, 1);
  at(105); assert.match(said[1], /end this call now/);
  ctx.modelGenerating = true; at(108); ctx.modelGenerating = false;  // Goodbye being spoken.
  at(110); assert.equal(ended.length, 1);
  // A reply after the check-in resets everything.
  Object.assign(ctx, { idleStage: 0, lastCallerActivity: 0, lastAgentActivity: 0 }); said.length = 0; ended.length = 0;
  at(200); assert.equal(said.length, 1);
  ctx.active(); at(230); at(259); assert.equal(said.length, 1); assert.equal(ended.length, 0);
  // Tool work in progress never counts as silence.
  ctx.toolsInFlight = 1; at(400); assert.equal(said.length, 1); ctx.toolsInFlight = 0;
  console.log('PASS: silent caller gets one check-in, then a goodbye and hang-up; speech or agent activity resets it');
}

// Reply watchdog: nudge once when a result is answered with silence from both the agent and the caller.
{
  const nudges = [];
  vm.runInContext("running = ready = true; modelGenerating = false; inputTurnOpen = false; toolsInFlight = 0; outputContext = null; replyWatchMs = 0;", context);
  context.nudgeLive = { sendClientContent: c => nudges.push(c.turns[0].parts[0].text) };
  vm.runInContext('live = nudgeLive;', context);
  context.setTimeout = fn => { queueMicrotask(fn); return 1; };
  const watch = vm.runInContext('watchForReply', context);
  vm.runInContext("agentSpokeAt = 0; lastCallerActivity = 0;", context);
  watch(Date.now() + 1000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nudges.length, 1, 'silence after a result is nudged');
  assert.match(nudges[0], /caller is waiting/);
  vm.runInContext("agentSpokeAt = Date.now() + 5000;", context);
  watch(Date.now() + 1000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nudges.length, 1, 'no nudge when the agent spoke');
  vm.runInContext("agentSpokeAt = 0; lastCallerActivity = Date.now() + 5000;", context);
  watch(Date.now() + 1000); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nudges.length, 1, 'no nudge when the caller spoke');
  assert.equal(vm.runInContext("needsReply({ name: 'save_notes' }, {})", context), false);
  assert.equal(vm.runInContext("needsReply({ name: 'verify_booking' }, { verified: true })", context), true);
  assert.equal(vm.runInContext("needsReply({ name: 'verify_booking' }, { notConfirmed: true })", context), false);
  vm.runInContext("replyWatchMs = 6000;", context);
  console.log('PASS: reply watchdog nudges a silent agent once, never when someone already spoke');
}

// Early tool calls before verification get a natural line to say, and every result is delivered normally.
{
  const sent = [];
  const ctx2 = vm.createContext({ ...context, sent });
  assert.match(vm.runInContext('WAIT_FOR_CODE', context), /No rush/);
  fetchImpl = async () => response(400, { error: 'Verify the caller with a one-time code before accessing booking details or making changes.' });
  vm.runInContext("running = ready = true; live = { sendToolResponse: r => { if (!r.functionResponses[0].willContinue) globalThis.__out = r.functionResponses[0]; } }; callerTurn = undefined; refusalDelayMs = 0;", context);
  await context.helpers.executeTool({ id: 'early-get-case', name: 'get_case', args: {} }, 'call');
  let out = vm.runInContext('globalThis.__out', context);
  assert.match(out.response.result.instruction, /No rush/);
  assert.notEqual(out.scheduling, 'SILENT', 'a quiet caller still gets the gentle line');
  // The caller speaks during the hold: the line is dropped (delivered as silent context).
  const stubbedTimer = context.setTimeout;
  context.setTimeout = (fn, ms) => setTimeout(fn, Math.min(ms, 5));  // A real, shortened timer so the hold can actually wait.
  vm.runInContext("refusalDelayMs = 60000; globalThis.__out = null;", context);
  const held = context.helpers.executeTool({ id: 'early-get-case-2', name: 'get_case', args: {} }, 'call');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext('globalThis.__out', context), null, 'held back while the caller may still be reading');
  vm.runInContext("lastCallerActivity = Date.now() + 1", context);
  await held;
  context.setTimeout = stubbedTimer;
  out = vm.runInContext('globalThis.__out', context);
  assert.equal(out.scheduling, 'SILENT');
  console.log('PASS: an early tool call is answered gently only if the caller stays quiet; speech cancels it');

// Blocking tools can return while their preceding spoken sentence is still playing.
for (const scenario of ['queued', 'late-audio', 'device-latency', 'interrupted', 'hang-up', 'new-call', 'stalled', 'empty', 'disconnected', 'cancelled']) {
  let now = 0, stoppedAt = null, spoken = 0;
  const timers = [], responses = [];
  const ctx = vm.createContext({
    Date: { now: () => now }, running: true, callId: 'handoff-call', pendingHandoff: null,
    ready: true, live: { sendToolResponse: response => { if (scenario === 'disconnected') throw new Error('Closed'); responses.push(response); } },
    retryTimer: null, rotationTimer: null, verificationNoticeTimer: null, clearTimeout() {},
    outputContext: { currentTime: 0, getOutputTimestamp: () => ({ contextTime: Math.max(0, now / 1000 - (scenario === 'device-latency' ? .5 : 0)) }) },
    nextPlay: scenario === 'empty' || scenario === 'late-audio' ? 0 : 2,
    playing: new Set(), setTimeout: fn => { timers.push(fn); },
    stop: async () => { stoppedAt = now; ctx.running = false; ctx.callId = undefined; },
    transcript() {}, status() {}, $: () => ({}), error() {},
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    speechSynthesis: { speak() { spoken++; } },
  });
  vm.runInContext(app.slice(app.indexOf('async function handoff('), app.indexOf("$('start').onclick")) + '\nthis.handoff = handoff;', ctx);
  Object.assign(ctx, { modelGenerating: false, activeTranscript: {}, turns: 0, agentSince: '',
    play() {}, stopPlayback() { throw new Error('Handoff audio must drain'); }, recordInputTranscription() {}, finishCallerTurn() {}, checkpoint() {} });
  const incoming = app.slice(app.indexOf('const content = message.serverContent;'), app.indexOf('// Transcription must be processed'));
  vm.runInContext(`this.receive = message => { ${incoming} };`, ctx);
  const result = { id: 'transfer', name: 'transfer_to_representative', response: { result: { handoff: true } } };
  const job = ctx.handoff('Forwarding your call.', scenario === 'cancelled' ? undefined : result);
  assert.equal(stoppedAt, null, `${scenario}: do not cut playback on tool completion`);
  if (!['disconnected', 'cancelled'].includes(scenario)) assert.equal(responses[0]?.functionResponses[0], result, 'return the blocking tool result before awaiting completion');
  ctx.receive({ serverContent: { outputTranscription: { text: 'The complete sentence.' } } });
  for (let step = 0; step < 650 && stoppedAt === null; step++) {
    now += 50; ctx.outputContext.currentTime = now / 1000;
    if (now === 100) {
      if (scenario === 'hang-up') ctx.running = false;
      if (scenario === 'new-call') ctx.callId = 'replacement-call';
    }
    // The transcript is already complete. The next audio chunk arrives after a long network gap.
    if (now === 1000 && scenario === 'late-audio') ctx.nextPlay = 3;
    if (now === 1500 && ctx.pendingHandoff && !['stalled', 'disconnected', 'cancelled'].includes(scenario)) {
      if (scenario !== 'empty') ctx.receive({ serverContent: { modelTurn: { parts: [{ inlineData: { data: 'AAAAAAAA' } }] } } });
      ctx.receive({ serverContent: { turnComplete: true } });
      if (scenario === 'interrupted') ctx.receive({ serverContent: { interrupted: true } });
    }
    if (scenario === 'stalled') { ctx.outputContext.currentTime = 0; ctx.outputContext.getOutputTimestamp = () => ({ contextTime: 0 }); }
    timers.splice(0).forEach(fn => fn());
    await Promise.resolve(); await Promise.resolve();
    if (!timers.length && stoppedAt === null) break;
  }
  await job;
  if (scenario === 'hang-up' || scenario === 'new-call') {
    assert.equal(stoppedAt, null); assert.equal(spoken, 0);
  } else {
    assert.equal(spoken, ['stalled', 'empty', 'disconnected', 'cancelled', 'interrupted'].includes(scenario) ? 1 : 0, 'no duplicate browser announcement after model speech');
    assert.ok(stoppedAt >= (scenario === 'stalled' ? 30000 : scenario === 'empty' ? 1500 : scenario === 'late-audio' ? 3000 : scenario === 'device-latency' ? 2500 : 2000), scenario);
    assert.ok(stoppedAt <= 30000, 'bounded wait');
  }
}
console.log('PASS: blocking handoff waits for streamed speech and device playback, with bounded failure fallback');
}

// Observability: each API call sends a W3C traceparent and queues a matching browser span; telemetry holds no caller words.
{
  let seen;
  fetchImpl = async (route, options) => { seen = options.headers.traceparent; return response(200, {}); };
  vm.runInContext("callId = 'call'; telemetryQueue = [];", context);
  await api('tool', { callId: 'call', requestId: 'r', name: 'get_case', args: {}, turnText: 'my code is 123456' });
  assert.match(seen, /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/);
  const queued = vm.runInContext('telemetryQueue', context);
  assert.equal(queued.length, 1);
  assert.equal(`00-${queued[0].span.traceId}-${queued[0].span.spanId}-01`, seen, 'browser span is the server span parent');
  assert.equal(queued[0].span.name, 'browser.tool');
  assert.doesNotMatch(JSON.stringify(queued), /123456/);
  console.log('PASS: API calls carry traceparent and record a matching browser span without caller content');
}

// Session view of the voice model: agent turns with token usage, interruptions and tool cancellations.
{
  vm.runInContext("callId = 'call'; telemetryQueue = []; agentTurnAt = 0; turnTokens = { input: 0, output: 0 }; callTokens = { input: 0, output: 0 };", context);
  const observe = vm.runInContext('observeLive', context);
  observe({ serverContent: { outputTranscription: { text: 'Hello' } } });
  observe({ usageMetadata: { promptTokenCount: 100, responseTokenCount: 20 }, serverContent: { turnComplete: true } });
  observe({ serverContent: { modelTurn: {} } });
  observe({ usageMetadata: { promptTokenCount: 150, responseTokenCount: 5 }, serverContent: { interrupted: true } });
  observe({ toolCallCancellation: { ids: ['a', 'b'] } });
  const names = [...vm.runInContext('telemetryQueue.map(q => q.span.name)', context)];
  assert.deepEqual(names, ['browser.agent_turn', 'browser.interrupted', 'browser.agent_turn', 'browser.tool_cancelled']);
  const turnsSeen = vm.runInContext('telemetryQueue.filter(q => q.span.name === "browser.agent_turn").map(q => q.span.attrs)', context);
  assert.equal(turnsSeen[0].inputTokens, 100);
  assert.equal(turnsSeen[1].interrupted, true);
  assert.deepEqual({ ...vm.runInContext('callTokens', context) }, { input: 250, output: 25 }, 'call tokens are the sum over responses');
  console.log('PASS: Live session telemetry records agent turns, token usage, interruptions and cancellations');
}

// "<no speech>"-style placeholders spoken by the model are flagged (boolean only) and the span marked as an error.
{
  vm.runInContext("telemetryQueue = []; agentTurnAt = 0; turnSpeech = '';", context);
  const observe = vm.runInContext('observeLive', context);
  observe({ serverContent: { outputTranscription: { text: '<no speech>{pause}' } } });
  observe({ serverContent: { turnComplete: true } });
  observe({ serverContent: { outputTranscription: { text: 'One moment while I take care of that.' } } });
  observe({ serverContent: { turnComplete: true } });
  const spans = [...vm.runInContext('telemetryQueue.map(q => q.span)', context)];
  assert.deepEqual(spans.map(s => [s.attrs.placeholder, s.status]), [[true, 'error'], [false, 'ok']]);
  assert.doesNotMatch(JSON.stringify(spans), /no speech|One moment/, 'spoken text is never recorded');
  console.log('PASS: placeholder speech is flagged without recording what was said');
}

// A reply to a pending quote that the agent ignores gets one nudge that always requires words; speech or a tool call prevents it.
{
  const nudges = [];
  context.quoteLive = { sendClientContent: c => nudges.push(c.turns[0].parts[0].text) };
  vm.runInContext("live = quoteLive; running = ready = true; pendingHandoff = null; modelGenerating = inputTurnOpen = false; toolsInFlight = 0; outputContext = null; selectedProposal = { id: 'quote' }; agentSpokeAt = lastToolAt = 0; replyWatchMs = 0;", context);
  const watch = vm.runInContext('watchQuoteReply', context);
  const reply = vm.runInContext("callerTurn = { id: 't', channel: 'voice', proposalId: 'quote' }", context);
  watch(reply, Date.now()); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nudges.length, 1);
  assert.match(nudges[0], /ask whether they would like you to go ahead/);
  assert.doesNotMatch(nudges[0], /silent|nothing/i);
  vm.runInContext('agentSpokeAt = Date.now() + 1000;', context);
  watch(reply, Date.now()); await new Promise(resolve => setImmediate(resolve));
  vm.runInContext('agentSpokeAt = 0; lastToolAt = Date.now() + 1000;', context);
  watch(reply, Date.now()); await new Promise(resolve => setImmediate(resolve));
  vm.runInContext("lastToolAt = 0; selectedProposal = null;", context);
  watch(reply, Date.now()); await new Promise(resolve => setImmediate(resolve));
  assert.equal(nudges.length, 1, 'no nudge after speech, a tool call, or without a pending quote');
  vm.runInContext("replyWatchMs = 6000; running = ready = false;", context);
  console.log('PASS: an ignored reply to a quote is nudged once, never when the agent spoke or acted');
}

// Withdrawing a quote after the caller spoke must not leave dead air: the result then asks for a spoken reply.
{
  const sent = [];
  context.withdrawLive = { sendToolResponse: r => sent.push(...r.functionResponses), sendClientContent() {} };
  vm.runInContext("live = withdrawLive; running = ready = true; pendingHandoff = null; callId = 'call'; toolRequests.clear(); outbox.clear();", context);
  fetchImpl = async () => response(200, { result: { withdrawn: true } });
  const turnWith = async spoken => {
    beginCallerTurn('Hmm, that is a lot.'); finishCallerTurn();
    await vm.runInContext('callerTurn.registered', context);
    vm.runInContext(`agentSince = ${JSON.stringify(spoken)};`, context);
    await context.helpers.executeTool({ id: crypto.randomUUID(), name: 'withdraw_quote', args: { proposalId: 'quote' } }, 'call');
    return sent.filter(r => r.name === 'withdraw_quote').at(-1);
  };
  const silentAgent = await turnWith('');
  assert.equal(silentAgent.scheduling, 'WHEN_IDLE');
  assert.match(silentAgent.response.result.instruction, /respond to what they said/);
  const spokenAgent = await turnWith('No problem, I will not make that change.');
  assert.equal(spokenAgent.scheduling, 'SILENT', 'no second reply when the agent already answered');
  vm.runInContext("running = ready = false; live = undefined;", context);
  console.log('PASS: a withdrawn quote gets a spoken reply only when the caller would otherwise hear silence');
}
