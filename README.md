# Skyway Airlines Voice

See [the enterprise architecture guide](docs/enterprise.md) or its [offline visual edition](docs/enterprise.html) for all tools, fallback diagrams, surge planning, technology tradeoffs, implemented controls and remaining production dependencies. Regenerate the HTML with `uv run python scripts/render_enterprise.py`.

A concept demo of a customer service voice agent, with a Python/FastAPI backend and server-owned LangGraph workflow. Gemini provides native audio; the browser streams audio directly to Google using short-lived, configuration-constrained tokens. The permanent API key stays on the server.

Reservations, SMS, flight inventory, prices, payments, refunds and telephone transfers are simulated. Changes to demo bookings are real database writes. No airline reservation system, payment processor or phone carrier is connected.

## Run locally

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 22.14+, and a Gemini key with access to `gemini-3.8-live`.

```sh
npm ci
uv sync --frozen          # Creates/uses the project .venv
# Copy .env.example to .env and set GEMINI_API_KEY.
npm start
```

Open http://localhost:3000 in Chrome or Edge and allow the microphone. The frontend source is in `frontend/`; only bundled `dist/` assets are served. Environment files, Python source and databases are never mounted as static files.

Each private browser session receives six bookings for Michael Jackson, including one disrupted flight. Give a confirmation number, give the first and last name, confirm the readback, then read the six-digit code shown under **Text messages**. Ask about flights, seats, cabins, meals, bags, assistance, pets, Skyway Rewards, travel credits or refunds. Approve a proposed change with a new clear yes after hearing its exact price. Asking for a representative plays a simulated handoff and ends the call.

## Workflow and failure behavior

`workflow.py` uses LangGraph to route each event through validation, identity verification, proposal creation, commit or handoff, then receipt storage. The application persists state between events in SQLite/libSQL. Each graph invocation is bounded and finishes inside one database transaction; there is no separate LangGraph checkpointer or long-running server agent. Spoken wording remains model-driven.

- The server requires a verified booking owned by the browser session. Codes expire after five minutes, permit five attempts and are one-use; successful verification lasts 12 hours. This fixture inbox is a demo trust anchor, not real customer identity proofing.
- `/api/turn` registers each finished caller turn, with what the agent said since the caller's previous turn, before the browser sends tool requests. Only a hash of the turn is stored, never the transcript or codes. A reply is bound to the quote it answers; whether it approves is decided when the agent asks to commit.
- **Approval is a typed model judgment, not a keyword rule.** `consent.py` sends an isolated, tool-free classifier the pending quote, the agent's words and the caller's reply. It returns `approve`, `decline`, `question` or `unclear`, whether the agent actually stated the quote and its price (`heardQuote`), and the caller's exact words as evidence. The server checks the evidence appears in the transcript and rejects anything malformed. A booking change needs a second, independent judge (a different model and a stricter prompt) to agree. Errors, timeouts (4 s), an open circuit or disagreement count as `unclear`; nothing fails open.
- **Cost:** judgments run only when something is being acted on: when the agent calls `confirm_action` or `resend_verification`, or while an identity readback is pending. The verifier runs only after the first judge approves, so a typical approval costs two short calls (about 500 input and 30 output tokens each) and a decline or question one. The browser re-sends the judged turn at commit; the server accepts it only if it matches the saved hash. Judgments run outside the database write transaction, and each caller turn is judged once.
- **Escalation ladder for unclear replies,** enforced per quote on the server: first a natural re-ask with the quote restated, then a closed question ("To make this change, please say yes. To leave your booking as it is, please say no."), then the keypad: "To confirm this change, please press 1. To cancel it, press 7." (a dial pad in the demo; 7 is far from 1 so a slip cannot cancel). At that step spoken approval is paused, but a spoken refusal still cancels, so a declined quote can never be confirmed by a later key press. A decline cancels the quote, a question keeps it open, and an unheard quote must be restated. Every committed change records how it was approved (judges and evidence, or keypad) in its receipt.
- Proposals expire after ten minutes; price, eligibility and booking version are checked again at commit. Unavailable or declined quotes return `requiresNewProposal`.
- When the caller declines or changes their mind, the agent silently calls `withdraw_quote`, which removes the quote (it can never change a booking), so the pending card disappears and a later reply cannot confirm it.
- **Silent callers:** if nobody speaks for 60 seconds while the agent is idle, the agent asks once whether the caller is still there; after 40 more seconds of silence it says goodbye and the page hangs up. Time the agent spends talking or waiting on a tool never counts as silence, and any caller speech, typing or key press resets the timer. As a server-side backstop against abandoned tabs, `/api/token` refuses to renew the voice session of a call with no caller activity for 10 minutes and marks it ended.
- **No microphone:** if the browser blocks or has no microphone, the call continues as a text-only call ("Microphone unavailable. Type to the agent below.") instead of failing.
- A texted code is announced once: whichever path tells the agent first (its own tool result or the turn result), the other is delivered silently.
- `(callId, requestId)` uniquely identifies a tool request. Arguments are fingerprinted; reusing an ID with different arguments fails. Booking writes, action receipts, tool results and call state commit atomically. Replaying the same request after a restart or lost response returns its saved outcome, including rejected approvals and failed code attempts.
- Cancellation is durable. Cancellation before execution prevents the request from running and supersedes pending approval. A completed transaction wins a cancellation race: its receipt remains visible. An interruption cannot undo a committed change.
- A representative handoff requires case notes in the same tool request. The workflow saves the brief, verified-booking status, completed-action summaries and any unapproved proposal atomically before ending the call; the demo displays the brief for review. Telephone transfer and a representative inbox remain simulated.
- Every call has a timestamped interaction timeline. Gemini silently appends customer goals, relevant context and next steps with `save_notes`; the workflow also records successful verification, policy lookups, proposals, completed changes and handoffs from authoritative tool results. Entries and receipts save atomically, survive booking verification and reconnects, and are not duplicated by request replay. The UI displays server timestamps in the viewer's local time. Notes share the call's 24-hour retention window; this is not a permanent CRM history.
- Browser requests time out after 25 seconds. Transient tool failures get at most three attempts with exponential backoff and jitter, keeping the same request ID. Before retrying, the browser queries `/api/tool-status`. If the outcome remains unknown, the agent must check the case or offer a representative and must not claim success or failure or create a replacement action.
- Server responses have a 22-second deadline; database operations have a 20-second budget, write transactions four seconds, remote database requests at most five seconds, token creation 15 seconds, and policy retrieval eight seconds. SQLite lock waits are capped at three seconds. Network libraries enforce connect/read deadlines; a response timeout does not prove a worker stopped or a remote commit failed, which is why receipts are mandatory.
- API sizes and tool schemas are validated. Same-origin JSON, HttpOnly cookies, booking ownership, database-backed exact rolling-window rate limits and a UUID link gate are enforced outside model instructions. Limits cover new sessions per IP, voice tokens per caller and IP, a shared daily voice-token budget, and authenticated API calls; exhaustion returns 429 with `Retry-After` and is not automatically retried. Shared HTTPS deployments refuse requests if the access key is missing. Logs contain tool name, elapsed time and outcome, excluding credentials and caller text.

The local database uses Python's `sqlite3`. Hosted libSQL uses the documented HTTP transaction protocol with explicit timeouts and no statement-level write retries. Existing tables and booking JSON remain compatible with the previous Node backend. The additional `tool_receipts` table initializes without clearing existing records; legacy pending proposals are invalidated because they lack fresh approval evidence. Back up the database before deployment. Do not run old and new backends concurrently against shared active calls.

Verification recovery: `resend_verification` reuses the booking identified in the current call, requires a fresh caller request, shares the initial-code rolling rate limit, and is replay-safe. Incomplete codes do not consume attempts. Verification and resending remain agent-driven: the caller reads the SMS aloud and asks the agent for a new code. Microphone capture uses browser gain control and displays a local level meter; this does not guarantee recognition accuracy. Unmarked transcription fragments are accumulated until an explicit end or the provider response/tool boundary. Corrected-reference readback remains model-generated; six-character validation prevents wrong-length lookup requests.

Identity readbacks: `request_verification` first prepares a readback of both passenger name and booking reference, using the natural name by default and phonetic spelling only for fields needing clarification. LangGraph advances a fresh caller turn that the classifier judged a confirmation to the simulated SMS send within the same database transaction, saving a stable receipt and turn result. Replayed turns and duplicate model requests reuse the sent result. Corrections, ambiguous replies and expired confirmations return recovery instructions. The browser reports the authoritative result to Gemini; it does not initiate a second send. Failed turn requests have bounded retries and an explicit spoken recovery instruction. Only actual code sends consume the SMS rolling limit; readback preparation does not. No booking change is authorized by this identity continuation. A real external SMS provider would require an outbox and provider idempotency; the current transaction guarantee covers the simulated inbox only.

## Policies and long calls

Human handoff explicitly uses Gemini's `BLOCKING` behavior and saves notes before announcing the transfer and ending the AI call. Other tools use `NON_BLOCKING`. The browser sends a silent `in_progress` response with `willContinue: true`, followed by the authoritative final response with `willContinue: false`. Progress uses `SILENT` so it cannot schedule a second acknowledgment; final results use `WHEN_IDLE`. Background notes remain silent. The agent requests work in the same response as one short acknowledgment so execution can overlap speech, and waits for final results before dependent calls or completion claims. Automatic verification notices wait for an idle speaking slot. Database transactions and fresh approval guards still control booking writes.

Speech detection uses low start/end sensitivity, 300 ms prefix padding and a 1.5-second silence window to reduce false interruptions and allow thinking pauses. Microphone echo cancellation and noise suppression remain enabled; automatic gain is disabled to avoid raising background noise during quiet intervals. The prompt waits silently for fillers/pauses and keeps English unless the caller explicitly requests another language. These are mitigations, not a guarantee that native audio will distinguish every hesitation from speech; no transcript letters are deleted. The optional proactive-audio field is omitted because the constrained-token API rejects it.

Booking/name readbacks use NATO words directly ("Romeo, two, Alpha") with pauses, avoiding the "R as in Romeo" construction. This reduces an observed substitution failure, but native audio remains model-generated: a transcript is not proof of correct pronunciation. Listen to recorded evaluation audio and confirm readbacks with the caller.

`policies.json` contains reviewed airline policy snapshots adapted for this fictional airline. The existing policy rules, topic review dates and 30-day freshness limit are retained. Lookup returns independent copies without network requests or external airline links. Once a topic is older than 30 days, the agent offers a representative instead of guessing fees or eligibility. Review and update snapshots before that deadline. These demo summaries do not establish real fare eligibility or provide comprehensive airline policy advice.

Session resumption, context compression, reconnect backoff and proactive connection renewal are enabled. During reconnects the browser pauses audio input instead of replaying old speech. Booking state, receipts and concise case notes persist; periodic checkpoints restore context. Reloading starts a new conversation while booking changes remain.

The transcript stays in browser memory and is bounded to 120 messages. The app does not record audio. Codes are hashed in verification storage; the simulated inbox retains them until expiry, and the caller reads them to Gemini. Tool receipts last for the call's 24-hour retention window; booking action receipts remain with the booking. Avoid real personal information.

## Observability

Open `/ops` (same private-link gate as the app) for metrics, traces, calls and logs.

- **Traces:** OpenTelemetry SDK (`telemetry.py`). Every browser API call sends a W3C `traceparent`, so one trace runs from the page through the HTTP request, each LangGraph node (`workflow.*`), approval judges (`consent.judge`), the Gemini token and policy calls, and database transactions. The dashboard shows each trace as a waterfall with its correlated logs.
- **Logs:** records on the app logger are stored with the active trace and span IDs.
- **Metrics:** derived from spans when queried (`ops.py`), following the span-metrics pattern: rate, errors and p50/p95/p99 duration per route, tool outcomes, approval-ladder results, judge latency, dependency and circuit-breaker health, and voice reply latency (end of the caller's turn to the agent's first audio, measured in the browser and sent to `/api/telemetry`).
- Spans and logs go to `telemetry_spans` and `telemetry_logs` in the app database through a single background writer, so telemetry never blocks or fails a request. Rows are kept for 24 hours. At production scale, export the same spans with OTLP to a collector (X-Ray, Datadog or Grafana Tempo) instead.
- Telemetry contains no transcripts, codes, keys or booking contents, and call IDs are hashed.

## Hosting

Build the browser with `npm ci && npm run build`, install Python dependencies with `uv sync --frozen`, then run the FastAPI application on your preferred Python host:

```sh
uv run uvicorn app:app --host 0.0.0.0 --port 3000 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Use HTTPS through a trusted reverse proxy. Set `APP_ORIGIN` to the exact public HTTPS origin and `ACCESS_KEY` to a random UUIDv4; invite viewers with `/?k=<ACCESS_KEY>`. Shared HTTPS deployments require the access key. Configure trusted proxy addresses for your host so the application sees the correct HTTPS scheme and caller IP. Never trust arbitrary client-supplied forwarding headers.

Local development defaults to `file:demo.sqlite`. A host with persistent storage may also use SQLite; multiple instances need a shared libSQL-compatible database such as Turso, configured with `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`. Include `dist/`, `instructions.txt` and `policies.json` with the Python application. Exclude environment files, local databases and test artifacts from deployment packages.

The access link is a bearer secret. Set provider spending limits and use a separate database for development. Back up booking data before deploying schema changes.

## Checks

```sh
npm test         # Offline unittest suite: workflow, replay, concurrency, rollback, API/transport boundaries
npm run build
npm run smoke    # Billable: real constrained token and Gemini voice audio
npm run smoke -- --flows # Also checks delayed async lookup speech and records a NATO readback
uv run --env-file .env python scripts/eval_consent.py                               # Billable: approval judges on labelled replies
uv run --env-file .env python scripts/eval_consent.py --cases consent_holdout.json  # Held-out replies never used for tuning
```

Verified during this migration: eleven Python test groups, frontend failure checks and browser asset build pass. Hosted libSQL verification, a simulated refund, durable replay and cancellation passed; a hosted Python token connected to Gemini and returned audio. Synthetic spoken input exposed Gemini's omitted `finished` flag; the frontend accepts finalized input events and excludes interim hypotheses. The extended smoke check received spoken audio during an eight-second policy lookup and checked a NATO readback transcript, saving its audio for listening. Human listening, broader voice-quality evaluations, load testing and multi-hour soak testing remain necessary before claiming production readiness.

Before connecting real booking, SMS or payment systems, add provider idempotency keys and an outbox/reconciliation worker: a local transaction cannot make an external side effect atomic. Also replace fixture identity with trusted contact delivery, provide maintained policy/fare feeds, operational monitoring and retention controls, and measure latency and voice-quality evaluations.

## Primary references

- [Gemini ephemeral tokens](https://ai.google.dev/gemini-api/docs/live-api/ephemeral-tokens)
- [Gemini asynchronous tools](https://ai.google.dev/gemini-api/docs/live-api/tools)
- [Final and interim transcriptions](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)
- [Gemini session management](https://ai.google.dev/gemini-api/docs/live-api/session-management)
- [LangGraph workflows](https://docs.langchain.com/oss/python/langgraph/workflows-agents)
- [Turso HTTP transaction protocol](https://docs.turso.tech/sdk/http/reference)
