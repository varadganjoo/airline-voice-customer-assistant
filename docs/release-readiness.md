# Release readiness — 20 October 2026

**Decision: BLOCKED for live customer production.** The implementation can be evaluated with synthetic data in a protected staging environment. Passing the checks below does not certify an enterprise deployment.

## Revision and environment

- Code revision: [e003678f84af26315e99a4c242dc625708dcc763](https://github.com/varadganjoo/airline-voice-customer-assistant/commit/e003678f84af26315e99a4c242dc625708dcc763).
- Complete CI evidence: [Quality and security run 39413170203](https://github.com/varadganjoo/airline-voice-customer-assistant/actions/runs/39413170203).
- CI environment: GitHub-hosted Ubuntu, Python 3.13 and Node 22. Exact runner and tool versions are retained in the job logs.
- Local environment: Windows, Python 3.11.0 and Node 22.14.0. Managed network restrictions deny loopback connections with WinError 10013 and deny outbound package/provider connections.
- Review scope: changed HTTP/deployment boundaries, operator access, telemetry intake/queueing, container and workflow configuration; connected booking ownership, approval and replay contracts are covered by the regression suite. This is not an independent exhaustive repository security assessment.

## Acceptance evidence

| Area | Status | Evidence and limits |
| --- | --- | --- |
| Complete automated behaviour | PASS | All **69 Python tests** and **17 frontend checks** passed in CI. This includes the API and telemetry tests that cannot run through the restricted local socket stack. |
| Frontend build and Python syntax | PASS | CI frontend bundle and compileall completed successfully. |
| Dependency consistency | PASS | Frozen uv sync and the regenerated hash-locked container requirements matched the committed lock. |
| Known dependency vulnerabilities | PASS, scoped | CI ran npm audit with a high-severity failure threshold and pip-audit 2.10.1 with hashed requirements. npm reported 0 vulnerabilities; pip-audit reported no known vulnerabilities. Freshness: this run, 20 October 2026. This does not assess unknown vulnerabilities or container OS packages. |
| Operator separation | PASS, scoped | A customer cookie cannot authorise /ops pages, assets or /api/ops endpoints. A separate credential is required and rotation takes effect on the next request. Enterprise SSO/MFA remains absent. |
| Booking and call ownership | PASS, scoped | Existing negative ownership tests pass. Added telemetry regression rejects a caller attaching events to another account's call. |
| Shared deployment configuration | PASS, scoped | Tests reject an invalid mode, unsafe origin, short/reused access credentials, missing provider key, identical approval models and missing/relative persistent database configuration. Production mode refuses startup. |
| HTTP protections | PASS, scoped | Tests cover host validation, HTTPS enforcement, spoofed forwarding headers, no-store, CSP, nosniff and referrer policy on denials. Transport/proxy configuration still needs deployment verification. |
| Telemetry overload | PASS | A stalled writer is bounded to 64 queued/in-flight batches. Excess batches are dropped; capacity is released after completion. Telemetry is diagnostic and may be lost. |
| Microphone-off mode | PASS | The frontend regression invokes the real capture function with microphone=off and verifies zero device requests. This does not prove voice quality. |
| Secret exposure checks | PASS, scoped | Tracked files exclude local environment files and databases. Manual provider-key/token/private-key patterns found no credentials in the publication candidate. No dedicated entropy/history scanner is installed locally. |
| Live Gemini conversation | BLOCKED | Existing local provider credentials were loaded privately, but the managed runtime cannot start a usable networked app or connect to the provider. No successful live session is claimed. |
| App screenshots, keyboard and responsive journeys | BLOCKED | Local application navigation was refused/timed out. Screenshots must come from an actual successful browser session before the requested public publication. |
| Adversarial live voice evaluation | BLOCKED | Deterministic input/schema, ownership, malformed-output and consent-failure regressions pass. No current live prompt-injection campaign, accent/noise benchmark or independent pronunciation review ran. |
| Container build/image scan | BLOCKED | Docker CLI exists, but the Docker Desktop Linux daemon is not running. Default base image tags still need approved digest pins, image scanning and provenance. |
| Restore, load, failover and alerting | BLOCKED | No representative load/soak run, hosted database restore exercise, regional failure test or alert delivery evidence. |
| Organisational controls | BLOCKED | No deploying organisation's identity, privacy, residency, retention, incident ownership, compliance or production approval evidence has been supplied. |

## Findings addressed

| Finding | Prerequisite and impact | Change and verification |
| --- | --- | --- |
| Shared customer/operator access — High for a real shared service | A person with a customer invitation could query global operational views. | The outer HTTP boundary requires a separate operator credential for every operational page/API/asset. Negative and credential-rotation tests pass. |
| Missing telemetry call ownership — Medium | An authenticated caller who obtained another call ID could misattribute diagnostic events to it. | The intake checks account ownership and call expiry before submission. A real two-account database regression passes. |
| Unbounded telemetry work — Medium | Slow database writes plus sustained intake could accumulate background work and consume process memory. | A non-blocking 64-batch semaphore bounds work; overload/recovery tests pass. Dropped-batch warnings contain no caller content. |
| Security headers omitted on early refusals — Medium | Denied/redirected requests could bypass the original response-header section. | A pure ASGI wrapper attaches headers outside the application, including denials and errors. |
| Unsafe deployment assumptions — High readiness gap | A demo could be treated as a real customer service despite fixture identity/payment/transfer behaviour. | Staging settings are validated, production mode is blocked and the README/runbook state the missing integrations explicitly. |

No application patch can turn the simulated airline, SMS, payment or transfer systems into real integrations.

## Agent permissions and abuse boundaries

| Surface | Current permission | Boundary and remaining risk |
| --- | --- | --- |
| Caller audio/typed turns | Conversational input to Gemini | Untrusted input. A browser can fabricate transcript events; it is not a trusted telephone gateway. |
| Declared tools | Eleven fixed tools for policy, verification, options, proposals, approval, notes and handoff | Server-side schema and workflow guards; no model-selected SQL, shell, files or arbitrary network destinations. |
| Booking writes | Only a verified booking owned by the session | Quote/version/price/eligibility checks and fresh approval; real provider side effects still need an outbox and reconciliation. |
| Policy summaries | Local allowlisted topics | No external-page fetch or URL-selection surface. Facts expire after 30 days and need an accountable policy owner. |
| Notes and tool results | Data returned to the conversational model | Escaped as text in the UI. Prompt instructions do not prove resistance to all indirect injection or prevent every spoken misstatement. |
| Provider credential | Backend-only environment value | The browser receives a constrained temporary token. Provider privacy, residency, retention and spend controls require organisational validation. |
| Operations | Dedicated operator credential | Global diagnostic visibility is appropriate only for authorised operators of this single-airline demo. Multi-tenant operator RBAC and SSO/MFA are not implemented. |
| Browser telemetry | Authenticated, bounded intake with call ownership | Browser measurements remain untrusted and can be fabricated. Do not use them as the sole compliance or financial audit record. |

## Data and retention

Use synthetic bookings only. Audio and conversation context are sent to the voice provider; transcripts remain bounded in browser memory. Verification codes are hashed in verification storage but appear in the synthetic inbox until expiry. Notes and tool receipts have a logical call expiry of 24 hours; physical call deletion is triggered when a new call is created. Telemetry is retained for approximately 24 hours with opportunistic deletion. Booking/action rows persist.

These mechanisms are not a complete retention/deletion programme. Before real data, establish schedules, deletion jobs, backups, encryption, access reviews and contractual provider handling. The six-digit note check is not comprehensive PII or payment-card redaction.

## Deployment and rollback runbook

1. Keep APP_ENV=staging and synthetic data. Generate distinct customer/operator credentials through a secret manager. Set an absolute persistent SQLite path or a shared authenticated libSQL database.
2. Build from the reviewed source revision with approved digest-pinned Node/Python bases. Scan the resulting image and record its digest. Run as non-root with a read-only root filesystem, dropped capabilities and explicitly provisioned writable storage.
3. Terminate HTTPS at a trusted reverse proxy. Restrict backend ingress and set FORWARDED_ALLOW_IPS to that proxy's specific addresses. Verify the host/scheme and Secure/HttpOnly/SameSite cookies through the real proxy.
4. Back up the database and prove restoration into a separate environment. The current change is additive; do not drop data to roll back.
5. Start a separate candidate instance. Verify /health/live, /health/ready, customer access restrictions, operator isolation, voice connection and a synthetic proposal/approval/replay/handoff journey.
6. Begin with an internal cohort. Watch error ratios, latency percentiles, unknown outcomes, verification failures, approval disagreements, dropped telemetry and provider spending. Set alert thresholds against measured baselines; none are certified by this code change.
7. Stop promotion on an authorisation failure, missing/duplicate receipt, unexpected booking mutation, persistent dependency failure or breached agreed SLO. Pause new writes with BOOKING_WRITES_ENABLED=false and restart/reload the service configuration; it is not an instantaneous remote switch.
8. Route traffic back to the previous compatible image and keep the existing database/receipts. Reconcile unknown outcomes instead of replaying financial operations. Retain evidence for the incident review.

The repository maintainer owns code changes. The deploying organisation must name accountable platform, identity, security, policy, data/privacy and service-operations owners before promotion.

## Remaining release gates

- Real, trusted customer identity and SMS delivery; carrier reservation/fare adapters; payments/refunds; operational transfer/callback destination.
- External-effect idempotency, transactional outbox, reconciliation and human exception handling.
- Enterprise customer/operator identity, SSO/MFA, role/tenant isolation and access revocation.
- Reviewed policy/fare updates, tested browser/voice accessibility and supported language/noise coverage.
- Current image and history-secret scans, SBOM/provenance, enforced branch protections and approved release artefacts.
- Tested backup restoration, capacity, provider quotas, spending limits, alert delivery and incident escalation.
- Privacy, residency, retention/deletion and contractual/compliance acceptance.

No production recommendation is made while these critical controls remain unverified.
