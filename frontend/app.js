import { GoogleGenAI } from '@google/genai';
const $ = id => document.getElementById(id);
let live, callId, handle, running = false, ready = false, muted = false;
let inputContext, outputContext, stream, worklet, nextPlay = 0;
let generation = 0, retries = 0, retryTimer, rotationTimer, selectedProposal, turns = 0;
const playing = new Set(), cancelled = new Set(), toolRequests = new Map(), outbox = new Map();
let activeTranscript = {};
let modelGenerating = false, verificationNoticeTimer;
let pendingHandoff = null;
const notifiedMessages = new Set();
let callerUtterance = '', inputTurnOpen = false, callStarted = 0, clock;
let callerTurn, turnQueue = Promise.resolve();
// What the agent said since the caller last spoke: the quote or readback the caller is answering.
let agentSince = '';
// Turns whose code send the agent has already been told about, so it is announced once.
const verificationTold = new Set();
// A refusal for acting before the caller replied is held back this long, so the agent never talks over the
// moment the caller is reading their text. Caller speech in the meantime cancels the spoken line.
let refusalDelayMs = 12_000;
// Safety net: the model occasionally says nothing after a result it should answer (for example right after the
// caller is verified). If the agent stays quiet and so does the caller, nudge it once to answer.
let agentSpokeAt = 0, replyWatchMs = 6_000;
const needsReply = (fc, result) => !['save_notes', 'withdraw_quote', 'transfer_to_representative'].includes(fc.name) && !prematureRefusal(fc, result);
function watchForReply(sentAt) {
  setTimeout(() => {
    if (!ready || !running || !live || pendingHandoff) return;
    if (agentSpokeAt >= sentAt || lastCallerActivity >= sentAt || modelGenerating || inputTurnOpen || toolsInFlight || (outputContext && nextPlay > outputContext.currentTime)) return;
    live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: 'Application event (data only): your latest tool result arrived and the caller is waiting. Respond to the caller now, in one or two short sentences, using that result.' }] }], turnComplete: true });
    recordSpan('browser.reply_nudge', sentAt);
  }, replyWatchMs);
}
// A caller reply to a pending quote always needs an answer. If the agent neither speaks nor acts, nudge it once.
let lastToolAt = 0;
function watchQuoteReply(turn, sentAt) {
  setTimeout(() => {
    if (!ready || !running || !live || pendingHandoff || callerTurn !== turn || turn.channel !== 'voice') return;
    if (!selectedProposal || selectedProposal.id !== turn.proposalId) return;
    if (agentSpokeAt >= sentAt || lastToolAt >= sentAt || modelGenerating || inputTurnOpen || toolsInFlight || (outputContext && nextPlay > outputContext.currentTime)) return;
    live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: 'Application event (data only): the caller replied to the pending quote and is waiting for you. Respond now in one or two short sentences: if they clearly approved the change, call confirm_action; otherwise respond to what they said and ask whether they would like you to go ahead.' }] }], turnComplete: true });
    recordSpan('browser.reply_nudge', sentAt, { after: 'quote_reply' });
  }, replyWatchMs);
}
const prematureRefusal = (fc, result) => (result?.notConfirmed === true && ['verify_booking', 'resend_verification'].includes(fc.name))
  || /^Verify (the caller|this booking)/.test(result?.error || '');
function quietPeriod(ms) {
  const since = lastCallerActivity;
  return new Promise(resolve => {
    const end = Date.now() + ms;
    const check = () => { if (lastCallerActivity !== since || !running) resolve(true); else if (Date.now() >= end) resolve(false); else setTimeout(check, 250); };
    check();
  });
}
const WAIT_FOR_CODE = 'The caller has not been verified yet. Do not call more tools. Say one short, warm sentence such as "No rush, I\'m here whenever you have the code," then wait for them to read it.';
// Silent-caller handling: check in after IDLE_CHECK_MS of silence, end the call IDLE_END_MS after that.
const IDLE_CHECK_MS = 60_000, IDLE_END_MS = 40_000;
let lastCallerActivity = 0, lastAgentActivity = 0, idleStage = 0, goodbyeSentAt = 0, toolsInFlight = 0, micAvailable = true;
// Call timer, like a phone's in-call screen.
const tick = () => { if (ready && callStarted) { const s = Math.floor((Date.now() - callStarted) / 1000); status(`On call · ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`); } };
function error(message) {
  $('error').textContent = /gemini|google|sqlite|turso|websocket|\bsdk\b|api.?key|model access/i.test(message) ? 'The service could not complete your request. Please try again.' : message;
  $('error').hidden = !message;
}
function status(text) { $('status').textContent = text; }
// Observability: every API call is a client span whose W3C traceparent the server continues, so one trace runs from
// this page through the workflow, approval judges and database. Voice timings are recorded as spans too.
// Spans carry timings, names and counts only; the server hashes the call ID.
let telemetryQueue = [], replyClock = 0, telemetrySentAt = 0, dialedAt = 0;
const hex = bytes => [...crypto.getRandomValues(new Uint8Array(bytes))].map(b => b.toString(16).padStart(2, '0')).join('');
function recordSpan(name, startMs, attrs = {}, ids = {}, spanStatus = 'ok') {
  if (!callId || telemetryQueue.length >= 200) return;
  telemetryQueue.push({ call: callId, span: { traceId: ids.traceId || hex(16), spanId: ids.spanId || hex(8), name, kind: ids.kind || 'internal',
    startMs, durationMs: Math.max(0, Date.now() - startMs), status: spanStatus, attrs } });
}
function flushTelemetry() {
  telemetrySentAt = Date.now();
  const byCall = new Map();
  for (const { call, span } of telemetryQueue.splice(0)) byCall.set(call, [...(byCall.get(call) || []), span]);
  for (const [call, spans] of byCall) for (let i = 0; i < spans.length; i += 50) {
    fetch('/api/telemetry', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ callId: call, spans: spans.slice(i, i + 50) }) }).catch(() => {});
  }
}
// Per-session view of the voice model: each agent turn (with its token usage), interruptions, tool cancellations.
// Live reports usage per model response, so a call's tokens are the sum over its responses.
let agentTurnAt = 0, turnTokens = { input: 0, output: 0 }, callTokens = { input: 0, output: 0 }, reconnectReason = '', turnSpeech = '';
const PLACEHOLDER = /[<{\[(]\s*(no[\s_-]*speech|silen|pause|wait)/i;
function observeLive(message) {
  const usage = message.usageMetadata, content = message.serverContent, cancelledIds = message.toolCallCancellation?.ids;
  if (usage) {
    for (const tally of [turnTokens, callTokens]) { tally.input += usage.promptTokenCount || 0; tally.output += usage.responseTokenCount || 0; }
  }
  if (cancelledIds?.length) recordSpan('browser.tool_cancelled', Date.now(), { count: cancelledIds.length });
  if (message.goAway) reconnectReason = 'provider_go_away';
  if (!content) return;
  if ((content.modelTurn || content.outputTranscription) && !agentTurnAt) agentTurnAt = Date.now();
  if (content.outputTranscription?.text) turnSpeech = (turnSpeech + content.outputTranscription.text).slice(-400);
  if (content.interrupted) recordSpan('browser.interrupted', Date.now(), { agentSpokeMs: agentTurnAt ? Date.now() - agentTurnAt : 0 });
  if ((content.turnComplete || content.interrupted) && (agentTurnAt || turnTokens.output)) {
    const placeholder = PLACEHOLDER.test(turnSpeech);
    recordSpan('browser.agent_turn', agentTurnAt || Date.now(), { interrupted: Boolean(content.interrupted), spoke: Boolean(agentTurnAt),
      inputTokens: turnTokens.input, outputTokens: turnTokens.output, placeholder }, {}, placeholder ? 'error' : 'ok');
    agentTurnAt = 0; turnTokens = { input: 0, output: 0 }; turnSpeech = '';
  }
}
// Time from the end of the caller's turn to the agent's first audio: the latency a caller actually hears.
function voiceReplied() {
  if (replyClock) { recordSpan('browser.voice_latency', replyClock, { turn: turns }); replyClock = 0; }
}
async function api(route, body) {
  const ids = { traceId: hex(16), spanId: hex(8), kind: 'client' }, startedAt = Date.now();
  let failed = false;
  try { return await request(route, body, `00-${ids.traceId}-${ids.spanId}-01`); }
  catch (e) { failed = true; throw e; }
  finally { recordSpan(`browser.${route.replaceAll('-', '_')}`, startedAt, { route: `/api/${route}` }, ids, failed ? 'error' : 'ok'); }
}
async function request(route, body, traceparent) {
  const response = await fetch(`/api/${route}`, { method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? { traceparent } : { 'Content-Type': 'application/json', traceparent }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(25_000) });
  let data;
  try { data = await response.json(); }
  catch {
    const failure = new Error('The service returned an unreadable response. Please check the saved case.');
    failure.retryable = true;
    throw failure;
  }
  if (!response.ok) {
    const failure = new Error(data.error || 'Request failed.');
    failure.retryable = data.retryable ?? [429, 502, 503, 504].includes(response.status);
    failure.ended = data.ended === true;
    throw failure;
  }
  return data;
}
const retryable = e => e.retryable || e.name === 'TimeoutError' || e.name === 'AbortError' || e instanceof TypeError;
const backoff = attempt => new Promise(resolve => setTimeout(resolve, 250 * 2 ** attempt + Math.random() * 150));
async function retryApi(route, body) {
  for (let attempt = 0; ; attempt++) {
    try { return await api(route, body); }
    catch (e) { if (!retryable(e) || attempt >= 2) throw e; await backoff(attempt); }
  }
}
function beginCallerTurn(value = '', channel = 'voice', keypadKey = null) {
  let finish;
  const agentText = agentSince.slice(-4000); agentSince = '';
  callerTurn = { id: crypto.randomUUID(), text: value, proposalId: selectedProposal?.id || null, agentText, channel, keypadKey, complete: false,
    finished: new Promise(resolve => { finish = resolve; }), finish: () => finish() };
  callerUtterance = value;
  return callerTurn;
}
function finishCallerTurn() {
  const turn = callerTurn, localCall = callId;
  if (!turn || turn.complete || !turn.text.trim()) return;
  turn.complete = true; inputTurnOpen = false;
  if (!modelGenerating) replyClock = Date.now();
  const job = turnQueue.then(() => retryApi('turn', { callId: localCall, turnId: turn.id, text: turn.text, proposalId: turn.proposalId, agentText: turn.agentText, channel: turn.channel, keypadKey: turn.keypadKey })).then(async data => {
    if (callId === localCall && callerTurn === turn && data.state) renderCase(data.state);
    if (callId === localCall && data.result?.verificationResult) {
      await reportVerification(data.result.verificationResult, turn, localCall);
    }
    return data;
  });
  turn.registered = job; turnQueue = job.catch(e => {
    if (callId === localCall) {
      error(e.message);
      if (ready && !pendingHandoff) live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: 'Application error: the caller turn could not be saved after bounded retries. Do not claim verification or code delivery. Explain the service problem and offer to retry or transfer to a representative.' }] }], turnComplete: true });
    }
  }); turn.finish();
  watchQuoteReply(turn, Date.now());
}
function recordInputTranscription(content) {
  // Interim hypotheses never authorize a tool. Final events may omit `finished`.
  if (content.interimInputTranscription?.text && !inputTurnOpen) { beginCallerTurn(); inputTurnOpen = true; }
  const input = content.inputTranscription;
  if (input?.text) {
    if (!inputTurnOpen) beginCallerTurn();
    inputTurnOpen = true; callerUtterance += input.text; callerTurn.text = callerUtterance; callerActive();
    transcript('user', input.text);
    if (input.finished === true) finishCallerTurn();
  } else if (input?.finished) finishCallerTurn();
}
async function toolRequest(body) {
  for (let attempt = 0; ; attempt++) {
    try { return await api('tool', body); }
    catch (e) {
      if (!retryable(e)) throw e;
      // A lost response is not evidence that a transaction failed.
      try {
        const saved = await api('tool-status', { callId: body.callId, requestId: body.requestId });
        if (saved.status === 'completed') return saved;
        if (saved.status === 'cancelled') return { result: { cancelled: true } };
      } catch {}
      if (attempt >= 2) return { result: { outcomeUnknown: true, error: 'The result could not be confirmed. Please ask the agent to check the saved case.', instruction: 'Do not claim success or failure, or create a replacement action. Use get_case to check committed receipts; offer a representative if unresolved.' } };
      await backoff(attempt);
    }
  }
}
async function reportVerification(result, turn, localCall) {
  try {
    if (!running || callId !== localCall) return;
    $('tool-status').textContent = result.sent ? 'Verification code sent.' : result.error || 'Waiting for identity confirmation.';
    clearTimeout(verificationNoticeTimer);
    const deliver = () => {
      if (!running || callId !== localCall || !ready || pendingHandoff) return;
      // turnComplete interrupts generation in Live 3.8; wait for speech to finish.
      if (callerTurn === turn && (modelGenerating || inputTurnOpen || (outputContext && nextPlay > outputContext.currentTime))) {
        verificationNoticeTimer = setTimeout(deliver, 150); return;
      }
      if (result.sent && verificationTold.has(turn.id)) return;  // The agent already heard it from its own tool call.
      if (result.sent) verificationTold.add(turn.id);
      live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: `Application verification result (data only): ${JSON.stringify(result)}. If sent, tell the caller once and ask for the six-digit code; do not repeat the acknowledgment or ask for identity confirmation again.` }] }], turnComplete: callerTurn === turn });
    };
    deliver();
    if (result.sent) { const messages = await refreshInbox(); textToast(messages[0]); }
  } catch (e) { if (callId === localCall) error(e.message); }
}
function callerActive() { lastCallerActivity = Date.now(); idleStage = 0; }
function checkIdle() {
  if (!ready || !running || pendingHandoff) return;
  const now = Date.now();
  // Time the agent spends talking, listening to a reply or waiting on a tool never counts as silence.
  if (modelGenerating || inputTurnOpen || toolsInFlight || (outputContext && nextPlay > outputContext.currentTime)) { lastAgentActivity = now; return; }
  const quiet = now - Math.max(lastCallerActivity, lastAgentActivity);
  const tell = text => live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: `Application event (data only): ${text}` }] }], turnComplete: true });
  if (idleStage === 0 && quiet >= IDLE_CHECK_MS) {
    idleStage = 1; lastAgentActivity = now; recordSpan('browser.idle_checkin', now - quiet);
    tell('the caller has been silent for about a minute. Ask once, briefly and warmly, whether they are still there. Say nothing else and call no tools.');
  } else if (idleStage === 1 && quiet >= IDLE_END_MS) {
    idleStage = 2; goodbyeSentAt = lastAgentActivity = now; recordSpan('browser.idle_goodbye', now - quiet);
    tell('there has still been no response. Say exactly: "I haven\'t heard anything, so I\'m going to end this call now. Please call us back anytime. Goodbye." Call no tools.');
  } else if (idleStage === 2 && ((lastAgentActivity > goodbyeSentAt && quiet >= 1500) || now - goodbyeSentAt > 15_000)) {
    idleStage = 3; void endCall('The call ended because there was no response.');
  }
}
async function endCall(message) {
  await stop(true, 'no_response');
  transcript('system', message, false); activeTranscript.system = null;
  status('Call ended · no response'); $('voice-hint').textContent = `${message} Call again whenever you're ready.`;
}
function text(parent, tag, value, className) { const el = document.createElement(tag); el.textContent = value; if (className) el.className = className; parent.append(el); return el; }
function transcript(role, value, append = true) {
  if (!value) return;
  const box = $('transcript'); box.querySelector('.empty')?.remove();
  if (!append || !activeTranscript[role]) {
    const p = document.createElement('p'); p.className = role;
    text(p, 'strong', role === 'user' ? 'YOU' : role === 'assistant' ? 'SKYWAY' : 'CALL');
    activeTranscript[role] = text(p, 'span', ''); box.append(p);
    while (box.children.length > 120) box.firstElementChild.remove();
  }
  activeTranscript[role].textContent += value;
  box.scrollTop = box.scrollHeight;
}
function renderCase(state) {
  renderNotes(state?.interactionNotes || []);
  selectedProposal = state?.proposals?.find(p => p.status === 'pending' && p.expiresAt > Date.now());
  $('proposal-card').hidden = !selectedProposal;
  // The server owns the approval step; the page only mirrors it.
  const step = selectedProposal?.approval?.mode || 'natural';
  promptKeypad(step === 'manual');
  if (!selectedProposal) return;
  $('proposal-text').textContent = `${selectedProposal.reference}: ${selectedProposal.summary}`;
  $('proposal-hint').textContent = step === 'manual' ? 'On the dial pad, press 1 to confirm this change or 7 to cancel it.'
    : step === 'closed' ? 'Please answer yes or no.' : 'Tell the agent in your own words whether to go ahead, or what you’d like instead.';
}
function renderNotes(notes) {
  const box = $('interaction-notes');
  // Independent tool responses can arrive out of order; never replace newer notes.
  const latest = notes.at(-1)?.timestamp || 0;
  if (latest < Number(box.dataset.latest || 0)) return;
  box.dataset.latest = String(latest);
  const following = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const position = box.scrollTop;
  box.replaceChildren();
  $('notes-count').textContent = String(notes.length);
  if (!notes.length) text(box, 'li', 'Notes will appear when your call starts.', 'empty');
  for (const note of notes) {
    const item = document.createElement('li');
    const meta = text(item, 'div', '', 'note-meta');
    text(meta, 'span', note.kind === 'agent' ? 'Agent note' : 'Call activity');
    const time = text(meta, 'time', new Date(note.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    time.dateTime = new Date(note.timestamp).toISOString();
    time.title = new Date(note.timestamp).toLocaleString();
    text(item, 'p', note.text);
    box.append(item);
  }
  box.scrollTop = following ? box.scrollHeight : position;
}
function renderHandoff(brief) {
  $('handoff-booking').textContent = brief.bookingVerified ? `Verified booking ${brief.bookingReference}` : 'Booking not verified';
  $('handoff-reason').textContent = brief.reason;
  $('handoff-notes').textContent = brief.notes;
  const actions = $('handoff-actions'); actions.replaceChildren();
  for (const action of brief.completedActions) text(actions, 'li', action);
  if (!brief.completedActions.length) text(actions, 'li', 'No booking changes completed.');
  $('handoff-pending').textContent = brief.unapprovedProposal ? `Not approved or applied: ${brief.unapprovedProposal}` : 'No pending change.';
  $('handoff-card').hidden = false;
}
function renderCaller(caller) {
  $('bookings').replaceChildren();
  text($('bookings'), 'strong', caller.name);
  for (const booking of caller.references) {
    const card = document.createElement('div'); card.className = 'booking';
    text(card, 'code', booking.reference); text(card, 'p', booking.trip); $('bookings').append(card);
  }
}
async function refreshInbox() {
  const { messages } = await api('inbox');
  $('inbox').replaceChildren();
  for (const message of messages.filter(m => m.kind === 'identity')) {
    const card = document.createElement('div'); card.className = 'booking';
    text(card, 'p', `To mobile ${message.to || 'on file'}: ${message.message}`); text(card, 'code', message.code); $('inbox').append(card);
  }
  return messages;
}
// Phone-style banner for an incoming text, so the caller sees the code arrive like a real SMS.
function textToast(message) {
  if (!message?.code) return;
  if (message.id && notifiedMessages.has(message.id)) return;
  if (message.id) { notifiedMessages.add(message.id); if (notifiedMessages.size > 32) notifiedMessages.delete(notifiedMessages.values().next().value); }
  const toast = document.createElement('button'); toast.type = 'button'; toast.className = 'toast'; toast.setAttribute('aria-label', 'Dismiss text message');
  const head = text(toast, 'span', '', 'toast-head'); text(head, 'span', 'Messages'); text(head, 'span', 'now');
  text(toast, 'strong', 'Skyway Airlines');
  const body = text(toast, 'span', '', 'toast-body'); text(body, 'b', message.code); body.append(' is your Skyway verification code. Skyway will only ask for this code when you call us.');
  if (message.to) text(toast, 'span', `To mobile ${message.to}`, 'toast-to');
  const dismiss = () => { toast.classList.add('leaving'); setTimeout(() => toast.remove(), 250); };
  toast.onclick = dismiss; setTimeout(dismiss, 15_000);
  $('toasts').prepend(toast);
}

function stopPlayback() { for (const source of playing) { try { source.stop(); } catch {} } playing.clear(); nextPlay = 0; $('orb').classList.remove('speaking'); }
function play(base64) {
  if (!outputContext || !running || !ready) return;
  voiceReplied();
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  if (bytes.byteLength % 2) return;
  const view = new DataView(bytes.buffer), buffer = outputContext.createBuffer(1, bytes.length / 2, 24000);
  const samples = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  if (nextPlay - outputContext.currentTime > 30) { stopPlayback(); error('Audio playback fell behind. Please ask the agent to repeat the last answer.'); }
  const source = outputContext.createBufferSource(); source.buffer = buffer; source.connect(outputContext.destination);
  nextPlay = Math.max(outputContext.currentTime + .03, nextPlay);
  source.start(nextPlay); nextPlay += buffer.duration; playing.add(source); $('orb').classList.add('speaking');
  source.onended = () => { playing.delete(source); if (!playing.size) $('orb').classList.remove('speaking'); };
}
async function microphone() {
  stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  inputContext = new AudioContext({ sampleRate: 16000 });
  if (inputContext.sampleRate !== 16000) throw new Error('This browser cannot capture 16 kHz audio. Try current Chrome or Edge.');
  await inputContext.audioWorklet.addModule('/audio-worklet.js');
  worklet = new AudioWorkletNode(inputContext, 'microphone');
  inputContext.createMediaStreamSource(stream).connect(worklet); worklet.connect(inputContext.destination);
  worklet.port.onmessage = ({ data }) => {
    if (!ready || !running || muted || pendingHandoff) return;
    const samples = new Int16Array(data);
    const rms = Math.sqrt(samples.reduce((sum, value) => sum + (value / 32768) ** 2, 0) / samples.length);
    $('mic-level').value = Math.min(1, rms * 8);
    const bytes = new Uint8Array(data);
    try { live.sendRealtimeInput({ audio: { data: btoa(String.fromCharCode(...bytes)), mimeType: 'audio/pcm;rate=16000' } }); }
    catch { reconnect(); }
  };
  await inputContext.resume();
}
function flushTools() {
  if (!ready || !live) return;
  for (const [id, response] of outbox) {
    if (!cancelled.has(id)) live.sendToolResponse({ functionResponses: [response] });
    outbox.delete(id);
  }
}
function toolResponse(fc, result, willContinue = false) {
  if (fc.name === 'transfer_to_representative') return { id: fc.id, name: fc.name, response: { result } };
  return { id: fc.id, name: fc.name, response: { result }, willContinue,
    scheduling: willContinue || ['save_notes', 'withdraw_quote'].includes(fc.name) ? 'SILENT' : 'WHEN_IDLE' };
}
async function executeTool(fc, callAtStart) {
  if (!fc.id || toolRequests.has(fc.id) || pendingHandoff) return;
  lastToolAt = Date.now();
  // Some provider transcriptions omit finished. A tool request ends that input turn.
  finishCallerTurn();
  const turnAtStart = callerTurn;
  const job = (async () => {
    if (!['save_notes', 'withdraw_quote'].includes(fc.name)) $('tool-status').textContent = `${fc.name.replaceAll('_', ' ')}…`;
    let result, acknowledgementTimer;
    try {
      if (turnAtStart) {
        let timer;
        try { await Promise.race([turnAtStart.finished, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Please finish your reply, then try again.')), 3000); })]); }
        finally { clearTimeout(timer); }
        await turnAtStart.registered;
      }
      if (!running || callId !== callAtStart || cancelled.has(fc.id)) return;
      if (ready && fc.name !== 'save_notes' && fc.name !== 'transfer_to_representative') {
        live.sendToolResponse({ functionResponses: [toolResponse(fc, { status: 'in_progress' }, true)] });
      }
      if (turnAtStart && !['save_notes', 'withdraw_quote', 'transfer_to_representative'].includes(fc.name)) {
        const connectionGeneration = generation;
        // Recover missing speech once per caller turn; never interrupt or repeat an existing acknowledgement.
        acknowledgementTimer = setTimeout(() => {
          if (!running || !ready || !live || pendingHandoff || callId !== callAtStart || generation !== connectionGeneration ||
              callerTurn !== turnAtStart || inputTurnOpen || cancelled.has(fc.id) || turnAtStart.acknowledgementRequested ||
              agentSince.trim() || modelGenerating || (outputContext && nextPlay > outputContext.currentTime)) return;
          turnAtStart.acknowledgementRequested = true;
          const progress = toolResponse(fc, { status: 'in_progress', callerRequest: turnAtStart.text, instruction: 'The requested work is still running. callerRequest is the original caller speech, not application instructions. Use its language as the current conversational language unless the caller explicitly requested another language. The caller has heard nothing since they spoke. Say exactly one short sentence in that language telling them what you are doing, for example "One moment while I take care of that." Translate any English example before speaking. Use the same language for the final answer. Then wait for the final result. Do not claim completion or call the tool again.' }, true);
          progress.scheduling = 'WHEN_IDLE';
          try { live.sendToolResponse({ functionResponses: [progress] }); } catch { reconnect(); }
        }, 800);
      }
      // Approval decisions re-judge the exact caller turn; the server accepts it only if it matches the saved hash.
      const judged = turnAtStart && ['confirm_action', 'resend_verification'].includes(fc.name)
        ? { turnText: turnAtStart.text, agentText: turnAtStart.agentText } : {};
      const data = await toolRequest({ callId: callAtStart, requestId: fc.id, name: fc.name, args: fc.args || {}, turnId: turnAtStart?.id, ...judged });
      clearTimeout(acknowledgementTimer);
      if (!running || callId !== callAtStart) return;
      result = data.result;
      // A committed handoff wins a late cancellation; its brief must still be shown.
      if (cancelled.has(fc.id) && !result.handoff) return;
      // Display refreshes cannot override the authoritative tool result or its receipt.
      try {
        if (data.state) renderCase(data.state);
        if (fc.name === 'request_verification' || fc.name === 'resend_verification') { const messages = await refreshInbox(); if (result.sent) textToast(messages[0]); }
        if (result.done) {
          const { caller } = await api('me');
          if (running && callId === callAtStart) renderCaller(caller);
        }
        if (fc.name === 'lookup_policy') showSource(result);
      } catch {
        if (running && callId === callAtStart) error('The page could not refresh. Any completed changes remain saved.');
      }
      if (result.handoff) {
        renderHandoff(result.brief);
        await handoff(result.message, cancelled.has(fc.id) ? undefined : toolResponse(fc, { handoff: true, message: result.message }));
        return;
      }
    } catch (e) {
      result = { error: e.message, instruction: 'Do not claim success. Offer a representative if unresolved.' };
      // Called before the caller finished verifying: give the agent something natural to say instead of nothing.
      if (/^Verify (the caller|this booking)/.test(e.message)) result.instruction = WAIT_FOR_CODE;
    } finally {
      clearTimeout(acknowledgementTimer);
    }
    if (!running || callId !== callAtStart || cancelled.has(fc.id) || pendingHandoff) return;
    if (fc.name === 'transfer_to_representative' && (result.error || result.outcomeUnknown)) {
      result.instruction = 'The transfer has not been confirmed. Explain that you could not complete it; do not claim the caller is connected or promise another transfer without invoking the tool. If the outcome is unknown, check the existing receipt before a new attempt.';
    }
    if (!['save_notes', 'withdraw_quote'].includes(fc.name) || result.error) $('tool-status').textContent = result.error || (result.notConfirmed ? 'Waiting for caller confirmation.' : result.outcomeUnknown ? 'Result unconfirmed; checking is required.' : `${fc.name.replaceAll('_', ' ')} complete`);
    // scheduling is a FunctionResponse field (only used by NON_BLOCKING tools), not part of the payload.
    const response = toolResponse(fc, result);
    // A silent withdrawal right after the caller spoke would leave them in dead air: give the agent a turn to answer.
    if (fc.name === 'withdraw_quote' && result.withdrawn && turnAtStart && !agentSince.trim()) {
      response.response.result = { ...result, instruction: 'The caller has heard nothing since they spoke. Nothing on the booking changed. In one or two short sentences, respond to what they said and ask how they would like to proceed.' };
      response.scheduling = 'WHEN_IDLE';
    }
    if (prematureRefusal(fc, result)) {
      const heldAt = Date.now(), callerSpoke = await quietPeriod(refusalDelayMs);
      if (callerSpoke) response.scheduling = 'SILENT';  // The caller spoke: nothing to say.
      recordSpan('browser.refusal_held', heldAt, { tool: fc.name, outcome: callerSpoke ? 'dropped_caller_spoke' : 'spoken' });
    }
    if (!running || callId !== callAtStart || cancelled.has(fc.id) || pendingHandoff) return;
    if (['request_verification', 'resend_verification'].includes(fc.name) && result.sent && turnAtStart) {
      if (verificationTold.has(turnAtStart.id)) response.scheduling = 'SILENT';  // Already announced from the turn result.
      else verificationTold.add(turnAtStart.id);
    }
    outbox.set(fc.id, response);
    try { flushTools(); } catch { reconnect(); }
    if (response.scheduling !== 'SILENT' && needsReply(fc, result)) watchForReply(Date.now());
  })();
  toolRequests.set(fc.id, job);
  toolsInFlight++; void job.finally(() => { toolsInFlight--; });
  // Bound browser bookkeeping for multi-hour calls; server owns authoritative actions.
  if (toolRequests.size > 128) toolRequests.delete(toolRequests.keys().next().value);
  await job;
}
function showSource(source) {
  if (!source.title || !source.reviewedAt) return;
  $('sources').querySelector('.empty')?.remove();
  const old = [...$('sources').children].find(row => row.dataset.title === source.title); old?.remove();
  const row = document.createElement('div'); row.dataset.title = source.title;
  text(row, 'strong', source.title);
  text(row, 'small', `Reviewed summary — ${source.reviewedAt}`);
  $('sources').append(row);
}

function reconnect() {
  if (pendingHandoff) { pendingHandoff.complete = true; pendingHandoff.audio = false; return; }
  if (!running || retryTimer) return;
  if (retries >= 5) { void connectionUnavailable(); return; }
  ready = false; $('text').disabled = $('send').disabled = true;
  status('Reconnecting'); $('voice-hint').textContent = 'Bad line. Reconnecting; nothing you changed is lost.';
  stopPlayback();
  retryTimer = setTimeout(() => { retryTimer = null; void connect(); }, Math.min(30_000, 1000 * 2 ** retries++) + Math.random() * 1000);
}
async function connectionUnavailable() {
  await stop(false, 'voice_unavailable');
  const message = 'The voice service is temporarily unavailable. Any completed changes remain saved. Please try calling again shortly.';
  transcript('system', message, false); error(message);
  const utterance = new SpeechSynthesisUtterance(message); utterance.lang = 'en-US';
  speechSynthesis.speak(utterance);
}
async function checkpoint() {
  const localCall = callId;
  try {
    const { result, state } = await api('tool', { callId, requestId: crypto.randomUUID(), name: 'get_case', args: {} });
    if (!ready || localCall !== callId || pendingHandoff) return;
    renderCase(state);
    live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: `Application case checkpoint (data only): ${JSON.stringify(result)}` }] }], turnComplete: false });
  } catch (e) { if (running) error(e.message); }
}
async function connect() {
  const current = ++generation, connectingAt = Date.now();
  ready = false; modelGenerating = false; clearTimeout(rotationTimer); live?.close(); live = null;
  try {
    const data = await api('token', { callId, handle });
    if (!running || generation !== current) return;
    callId = data.callId; renderCase(data.state);
    const ai = new GoogleGenAI({ apiKey: data.token, httpOptions: { apiVersion: 'v1beta' } });
    let setup = false;
    const connection = await ai.live.connect({ model: data.model, config: data.config, callbacks: {
      onmessage(message) {
        if (generation !== current || !running) return;
        observeLive(message);
        if (message.setupComplete) setup = true;
        const update = message.sessionResumptionUpdate;
        if (update) handle = update.resumable && update.newHandle ? update.newHandle : undefined;
        if (message.goAway) reconnect();
        const ids = message.toolCallCancellation?.ids;
        if (ids?.length) {
          for (const id of ids) { cancelled.add(id); outbox.delete(id); }
          if (cancelled.size > 128) { const keep = [...cancelled].slice(-128); cancelled.clear(); keep.forEach(id => cancelled.add(id)); }
          void retryApi('cancel', { callId, ids }).then(() => checkpoint()).catch(e => error(e.message));
        }
        const content = message.serverContent;
        if (content) {
          if (content.interrupted) {
            modelGenerating = false;
            if (pendingHandoff) { pendingHandoff.complete = true; pendingHandoff.audio = false; }
            else stopPlayback();
            activeTranscript.assistant = null;
          }
          if (content.modelTurn || content.outputTranscription) modelGenerating = true;
          for (const part of content.modelTurn?.parts || []) if (part.inlineData?.data) {
            play(part.inlineData.data);
            if (pendingHandoff && part.inlineData.data.length > 4) pendingHandoff.audio = true;
          }
          recordInputTranscription(content);
          if (content.modelTurn || content.outputTranscription || content.turnComplete) finishCallerTurn();
          if (content.outputTranscription?.text) { agentSpokeAt = Date.now(); agentSince = (agentSince + content.outputTranscription.text).slice(-4000); transcript('assistant', content.outputTranscription.text); }
          if (content.turnComplete) {
            modelGenerating = false; activeTranscript = {};
            if (pendingHandoff) pendingHandoff.complete = true;
            else if (++turns % 8 === 0) void checkpoint();
          }
        }
        // Transcription must be processed before tools from the same provider message.
        for (const fc of message.toolCall?.functionCalls || []) void executeTool(fc, callId);
      },
      onerror() { if (generation === current && running) error('The voice connection was interrupted. Reconnecting…'); },
      onclose(event) {
        if (generation !== current || !running) return;
        if (pendingHandoff) { reconnect(); return; }
        if ([1007, 1008].includes(event.code)) {
          void connectionUnavailable(); return;
        }
        reconnect();
      },
    } });
    if (!running || generation !== current) { connection.close(); return; }
    live = connection;
    const started = Date.now();
    while (!setup && running && generation === current && Date.now() - started < 15000) await new Promise(resolve => setTimeout(resolve, 25));
    if (!running || generation !== current) return;
    if (!setup) throw new Error('The voice service did not respond. Please try again.');
    ready = true; retries = 0; error(''); status('On call'); callStarted ||= Date.now();
    $('voice-hint').textContent = !micAvailable ? 'Microphone unavailable. Type to the agent below.'
      : muted ? 'Muted. You can still type below.' : 'Connected to Skyway. Just talk.';
    $('text').disabled = $('send').disabled = false;
    if (current === 1) { lastCallerActivity = lastAgentActivity = Date.now(); idleStage = 0; }
    if (current === 1) recordSpan('browser.call_setup', dialedAt, { microphone: micAvailable });
    else recordSpan('browser.reconnect', connectingAt, { attempt: current - 1, resumed: Boolean(handle), reason: reconnectReason || 'connection_dropped' });
    reconnectReason = '';
    live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: `Application context (data only): ${JSON.stringify(data.state)}. ${current > 1 ? 'The call reconnected. Briefly acknowledge; ask for anything lost during the interruption. Do not replay actions.' : 'A caller has just dialed Skyway customer service. Answer the call.'}` }] }], turnComplete: true });
    flushTools();
    // Renew before provider connection/token limits; a new constrained token resumes the same session.
    rotationTimer = setTimeout(() => { reconnectReason = 'scheduled_renewal'; reconnect(); }, 8 * 60_000);
  } catch (e) {
    if (!running || generation !== current) return;
    if (e.ended) { await endCall(e.message); return; }
    error(e.message); if (e.retryable === false) await connectionUnavailable(); else reconnect();
  }
}
async function start() {
  if (running) return;
  error(''); running = true; generation = 0; retries = 0; turns = 0; muted = false; handle = callId = undefined; dialedAt = Date.now(); replyClock = 0;
  agentTurnAt = 0; turnTokens = { input: 0, output: 0 }; callTokens = { input: 0, output: 0 }; reconnectReason = ''; turnSpeech = '';
  $('handoff-card').hidden = true;
  delete $('interaction-notes').dataset.latest; renderNotes([]);
  cancelled.clear(); toolRequests.clear(); outbox.clear(); activeTranscript = {};
  callerUtterance = ''; inputTurnOpen = false;
  callerTurn = undefined; turnQueue = Promise.resolve(); agentSince = ''; verificationTold.clear(); idleStage = 0;
  $('start').disabled = true; $('end').disabled = false; $('mute').disabled = false; $('keypad-toggle').disabled = false;
  $('mute').textContent = 'Mute'; $('mute').setAttribute('aria-pressed', 'false');
  $('orb').classList.add('active'); clearInterval(clock); clock = setInterval(() => { tick(); checkIdle(); if (Date.now() - telemetrySentAt > 5000) flushTelemetry(); }, 1000); status('Ringing…');
  try {
    outputContext = new AudioContext(); await outputContext.resume();
    await startup; renderCaller((await api('me')).caller);
    if (!running) return;
    try { await microphone(); micAvailable = true; }
    catch (e) {
      if (!['NotAllowedError', 'NotFoundError', 'NotReadableError', 'SecurityError'].includes(e.name)) throw e;
      micAvailable = false; stream?.getTracks().forEach(track => track.stop()); stream = null; $('mute').disabled = true;
    }
    if (running) await connect(); else await stop();
  }
  catch (e) { error(e.message); await stop(true, 'setup_failed'); }
}
async function stop(notify = true, reason = 'caller_hung_up') {
  const oldCall = callId;
  if (oldCall) recordSpan('browser.call_end', callStarted || dialedAt, { reason, turns, connected: Boolean(callStarted),
    inputTokens: callTokens.input, outputTokens: callTokens.output });
  pendingHandoff = null;
  running = ready = modelGenerating = false; generation++; clearTimeout(verificationNoticeTimer); clearTimeout(retryTimer); retryTimer = null; clearTimeout(rotationTimer);
  live?.close(); live = null; stopPlayback(); speechSynthesis.cancel();
  stream?.getTracks().forEach(track => track.stop()); stream = null;
  worklet?.disconnect(); worklet = null;
  await inputContext?.close().catch(() => {}); await outputContext?.close().catch(() => {}); inputContext = outputContext = null;
  callId = handle = undefined; $('start').disabled = false; $('end').disabled = $('mute').disabled = $('text').disabled = $('send').disabled = $('keypad-toggle').disabled = true;
  showDialpad(false); $('dialpad-display').textContent = '';
  $('orb').classList.remove('active'); $('proposal-card').hidden = true; status('Call ended'); callStarted = 0; clearInterval(clock); $('voice-hint').textContent = 'Call Skyway whenever you’re ready.';
  $('mic-level').value = 0;
  if (notify && oldCall) { try { await api('end', { callId: oldCall }); } catch (e) { error(`Audio ended. Could not close the saved case: ${e.message}`); } }
  flushTelemetry();
}
async function handoff(message, response) {
  if (pendingHandoff) return;
  const handoffCall = callId, deadline = Date.now() + 30_000;
  const closing = pendingHandoff = { complete: false, audio: false };
  let drained = false;
  clearTimeout(retryTimer); clearTimeout(rotationTimer); clearTimeout(verificationNoticeTimer);
  $('text').disabled = $('send').disabled = $('keypad-toggle').disabled = $('mute').disabled = true;
  try {
    if (!response || !ready || !live) closing.complete = true;
    else live.sendToolResponse({ functionResponses: [response] });
  } catch { closing.complete = true; }
  // Empty buffers or complete text do not mean the voice stream has finished.
  while (running && callId === handoffCall && Date.now() < deadline) {
    // Wait for the speaker's presentation clock, not just the Web Audio render clock.
    const playedUntil = outputContext?.getOutputTimestamp?.().contextTime ??
      (outputContext ? outputContext.currentTime - (outputContext.baseLatency || 0) - (outputContext.outputLatency || 0) : Infinity);
    if (closing.complete && !playing.size && nextPlay <= playedUntil) { drained = true; break; }
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  if (!running || callId !== handoffCall) return;
  await stop(false, 'handoff');
  transcript('system', message, false); status('Handoff prepared');
  $('tool-status').textContent = 'Handoff notes saved. AI call ended.';
  if (drained && closing.audio) return;
  const utterance = new SpeechSynthesisUtterance(message); utterance.lang = 'en-US';
  utterance.onerror = () => error('Handoff speech could not play. The exact handoff message is shown in the transcript.');
  speechSynthesis.speak(utterance);
}
$('start').onclick = () => void start();
$('end').onclick = () => void stop();
$('mute').onclick = () => {
  muted = !muted; stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
  $('mute').textContent = muted ? 'Unmute' : 'Mute'; $('mute').setAttribute('aria-pressed', String(muted));
  $('voice-hint').textContent = muted ? 'Muted. You can still type below.' : 'Connected to Skyway. Just talk.';
  if (muted) $('mic-level').value = 0;
  if (muted && ready) live.sendRealtimeInput({ audioStreamEnd: true });
};
$('clear').onclick = () => { $('transcript').replaceChildren(); activeTranscript = {}; };
// Demo dial pad. On a real call these are touch tones (DTMF) delivered by the phone platform, not speech.
const DTMF = { 1: [697, 1209], 2: [697, 1336], 3: [697, 1477], 4: [770, 1209], 5: [770, 1336], 6: [770, 1477],
  7: [852, 1209], 8: [852, 1336], 9: [852, 1477], '*': [941, 1209], 0: [941, 1336], '#': [941, 1477] };
function touchTone(key) {
  if (!outputContext) return;
  const gain = outputContext.createGain(), end = outputContext.currentTime + .15;
  gain.gain.value = .06; gain.connect(outputContext.destination);
  for (const frequency of DTMF[key]) {
    const tone = outputContext.createOscillator(); tone.frequency.value = frequency;
    tone.connect(gain); tone.start(); tone.stop(end);
  }
}
function showDialpad(open) {
  $('dialpad').hidden = !open; $('keypad-toggle').setAttribute('aria-expanded', String(open));
}
function promptKeypad(active) {
  $('dialpad').querySelector('[data-key="1"]').classList.toggle('prompt', active);
  $('dialpad').querySelector('[data-key="7"]').classList.toggle('prompt-cancel', active);
  if (active && running) showDialpad(true);
}
$('keypad-toggle').onclick = () => showDialpad($('dialpad').hidden);
$('dialpad').querySelector('.dialpad-keys').onclick = e => {
  const key = e.target.closest('button')?.dataset.key;
  if (!key || !running) return;
  touchTone(key);
  $('dialpad-display').textContent = ($('dialpad-display').textContent + key).slice(-12);
  // Only 1 (confirm) and 7 (cancel) at the keypad step of the approval ladder mean anything; other presses are just tones.
  if ((key === '1' || key === '7') && selectedProposal?.approval?.mode === 'manual') void keypadPress(key);
};
// Last step of the approval ladder: a keypad press, independent of speech recognition.
async function keypadPress(key) {
  if (!ready || !selectedProposal || pendingHandoff) return;
  callerActive(); recordSpan('browser.keypad', Date.now(), { key: key === '1' ? 'confirm' : 'cancel' });
  const proposalId = selectedProposal.id, turn = beginCallerTurn(`Keypad ${key}`, 'keypad', key);
  finishCallerTurn(); transcript('system', key === '1' ? 'Keypad: 1 pressed to confirm.' : 'Keypad: 7 pressed to cancel.', false);
  activeTranscript.system = null;
  try { await turn.registered; } catch { return; }
  if (!ready || callerTurn !== turn) return;
  // 7 is applied by the server when the turn is registered; the agent only needs to acknowledge it.
  const event = key === '1'
    ? `the caller pressed 1 on the keypad to confirm the pending change ${proposalId}. Call confirm_action for it now and report the result.`
    : 'the caller pressed 7 on the keypad to cancel the pending change. Nothing was changed and the quote is cancelled. Acknowledge that briefly and ask what they would like instead.';
  live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: `Application event (data only): ${event}` }] }], turnComplete: true });
}
$('text-form').onsubmit = e => {
  e.preventDefault(); const value = $('text').value.trim(); if (!value || !ready || pendingHandoff) return;
  beginCallerTurn(value); finishCallerTurn(); callerActive();
  stopPlayback(); transcript('user', value, false); activeTranscript.user = null; $('text').value = '';
  live.sendClientContent({ turns: [{ role: 'user', parts: [{ text: value }] }], turnComplete: true });
};
window.addEventListener('pagehide', () => { if (callId) recordSpan('browser.call_end', callStarted || dialedAt, { reason: 'page_closed', turns }); flushTelemetry(); running = false; live?.close(); stream?.getTracks().forEach(t => t.stop()); });
// Show the caller's trips before they dial; 'start' reuses the session or issues fresh trips.
// A private link carries ?k=<key>; the server swaps it for a cookie, so drop it from the address bar.
const accessKey = new URLSearchParams(location.search).get('k');
const startup = api('start', accessKey ? { key: accessKey } : {}).then(data => {
  renderCaller(data.caller);
  if (accessKey) history.replaceState(null, '', location.pathname);
  return data;
});
void startup.catch(e => error(e.message));
