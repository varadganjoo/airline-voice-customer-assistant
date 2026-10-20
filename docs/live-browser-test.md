# Live browser test - 20 October 2026

## Environment and scope

- Source: enterprise-readiness code revision b47c25696c362efc08894d1aad9407f696150ff4; subsequent changes contain documentation and captures only.
- Browser: connected desktop Chrome on Windows, actual application at http://localhost:3001/?microphone=off.
- Runtime: npm start in the maintainer's terminal, CPython 3.14.7 selected by uv; development mode and an ignored local SQLite database.
- Provider: existing server-side Gemini credential, reused locally without committing it. A real Live session produced assistant responses and invoked application tools.
- Input: typed caller turns only. The microphone-off capture guard ran before any device request; the UI displayed "Microphone unavailable. Type to the agent below." The existing regression separately checks zero getUserMedia calls.
- Data: generated sample bookings for Michael Jackson. All displayed reservation details, inbox messages and verification codes are fixtures, not customer records. Codes are short-lived and one-use.

## Observed journey

| Step | Expected | Observed |
| --- | --- | --- |
| Start session | Generic airline greeting and no microphone capture | Connected; assistant introduced Skyway Airlines; microphone unavailable and typed input enabled. |
| Ask carry-on rules | Avoid making up missing policy | Assistant said the specific carry-on details were unavailable and offered a representative. |
| Ask checked-bag limits | Use the reviewed snapshot | Answer: 62 linear inches; 50 lb Economy/Premium Economy, 70 lb Business/First Class. The UI showed the 29 September 2026 review date. |
| Identify E3M7N6 | Read back identity before code delivery | Assistant read back Michael Jackson and the reference; caller confirmed. |
| Verify | Require the fixture inbox code | Code delivery appeared in the synthetic inbox, the caller supplied it and verification completed. |
| Read seat options | No booking mutation | Available First Class options included 3F and 4D for outbound SW 2154 DEN-SFO. |
| Quote 3F, withhold approval | Preserve current seat | Proposal displayed 2F to 3F at no charge and waited for approval. |
| Ask "Is that really free?" | A price question must not authorise execution | Assistant clarified; proposal remained pending. Read-only SQLite check: version 0, outbound 2F, zero actions. |
| Explicitly approve exact quote | One durable change | Completed; read-only SQLite check: version 1, outbound 3F, exactly one action. |
| Request a representative | Save context before simulated transfer | Session ended as "Handoff prepared"; brief included verified reference, completed seat change, unresolved carry-on question and no pending change. |
| Readiness | Meet the current readiness check | /health/ready returned status ready and bookingWritesEnabled true for this development instance. |
| Keyboard | Disclosure reachable and operable | Tab reached Policies checked; Enter collapsed it. Notes and handoff regions were focusable. |
| Responsive | No horizontal document overflow | 390 x 844 viewport override produced a stacked layout; document clientWidth and scrollWidth were both 375 CSS pixels (scrollbar excluded). Override reset afterwards. |

## Captures

1. [Connected microphone-off session](screenshots/microphone-off-session.jpg)
2. [Reviewed checked-baggage guidance](screenshots/reviewed-policy.jpg)
3. [Proposal still awaiting approval](screenshots/approval-required.jpg)
4. [Saved seat change](screenshots/saved-seat-change.jpg)
5. [Representative handoff brief](screenshots/handoff-brief.jpg)
6. [Mobile workspace](screenshots/mobile-workspace.jpg)

Screenshots are unedited browser captures. They show a real provider conversation against simulated airline services. Capture timestamps in the app use the browser's local time.

## Limits

This was one synthetic journey, not a complete voice evaluation. No microphone speech was captured; pronunciation, listening quality, accents, barge-in under actual speech and noisy-room performance were not assessed. Audio output was not independently scored. No real airline reservation, SMS delivery, payment, refund or telephone transfer took place. Keyboard and responsive checks were basic, not a WCAG audit. No current live adversarial campaign, load/soak run or container validation was performed. Production remains blocked for the reasons in [release-readiness.md](release-readiness.md).
