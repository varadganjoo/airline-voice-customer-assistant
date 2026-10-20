"""Behavioral checks: workflows, durable replay, rollback, isolation and HTTP contracts."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx

from app import app, prepare_database
from database import RateLimitExceeded, Store, digest
from db_transport import DatabaseFailure, HttpDatabase
from tools import trip_label, assign_seat, decide, generate_booking, get_options, lookup_policy, now_ms, plan, propose
from voice import identity_readback, live_config, validate_tool
from workflow import process_event
import telemetry


# Deterministic stand-in for the model judges (consent.assess). The real judges are covered by
# test_consent.py (mocked provider) and scripts/eval_consent.py (labelled live evaluation).
YES = {"yes", "yes, go ahead", "yes please", "yes, that's correct", "yes, that's correct.", "yeah, that's correct.",
       "that's correct.", "yes, correct.", "yes, that is correct", "yes, please go ahead.", "yes, go ahead.",
       "yeah, that would be great.", "yes, please proceed."}
DECLINE = {"no", "no, you misheard my name.", "no, the last name is different."}
QUESTION = {"yes, but wait", "how much is that?"}
RESEND = {"please resend the verification code now.", "please send a new code."}
JUDGED = []


def fake_assess(kind, subject, agent_text, caller_text, mode="natural"):
    JUDGED.append((kind, caller_text, mode))
    text = caller_text.strip().lower()
    if kind == "resend":
        decision = "approve" if text in RESEND or (text in YES and subject.startswith("The agent offered")) else "unclear"
    elif mode == "closed":
        decision = "approve" if text == "yes" else "decline" if text == "no" else "unclear"
    else:
        decision = "approve" if text in YES else "decline" if text in DECLINE else "question" if text in QUESTION else "unclear"
    if decision == "approve" and kind != "resend" and not agent_text:
        decision = "notHeard"
    return {"decision": decision, "judges": [{"role": "judge", "model": "fake", "decision": decision, "heardQuote": bool(agent_text), "evidence": caller_text}]}


def booking():
    r = generate_booking("Michael Jackson", route=("DEN", "SFO"))
    for s in r["segments"]:
        s.update(cabinCode="economy", cabin="Economy", seat="", seatFeeUSD=0)
        s["seat"] = assign_seat(s, "economy")
    return r


class BackendTests(unittest.TestCase):
    def test_write_pause_blocks_change_but_keeps_booking_readable(self):
        self.verified()
        self.turn("Cancel and refund my booking")
        p = self.tool("propose_action", {"action": "cancel_and_refund", "segment": "", "value": ""})["result"]
        self.turn("yes", p["id"])
        with patch.dict(os.environ, {"BOOKING_WRITES_ENABLED": "false"}):
            self.assertIn("temporarily unavailable", self.tool("confirm_action", {"proposalId": p["id"]})["result"]["error"])
            self.assertIn("reservation", self.tool("get_case", {})["result"])
        self.assertEqual(self.store.lookup(self.account["id"], self.reference)["actions"], [])

    def test_clear_resend_is_judged_once_and_replay_does_not_send_again(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("Yes, that's correct")
        self.turn("Please resend the verification code now.")
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1, "a turn alone never sends")
        request = str(uuid4())
        sent = self.tool("resend_verification", {}, request)["result"]
        self.assertTrue(sent["sent"])
        self.assertEqual(self.tool("resend_verification", {}, request)["result"], sent)
        self.assertTrue(self.tool("resend_verification", {})["result"]["sent"], "same turn returns the same send")
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 2)

    def test_consent_is_whole_reply_and_pause_keeps_identity(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("Hold on, let me check")
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])
        self.turn("Yes, that is correct")
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1)

    def test_resend_yes_requires_pending_offer(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("yes")
        self.turn("yes")
        self.assertTrue(self.tool("resend_verification", {})["result"]["notConfirmed"])
        self.turn("yes please")
        self.assertTrue(self.tool("resend_verification", {})["result"]["sent"], "accepting the offer sends")
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="airline-python-test-")
        self.url = str(Path(self.temp.name) / "test.sqlite")
        self.env = patch.dict(os.environ, {"APP_ENV": "development", "TURSO_DATABASE_URL": self.url, "TURSO_AUTH_TOKEN": "", "GEMINI_API_KEY": "", "ACCESS_KEY": "", "APP_ORIGIN": ""})
        self.env.start()
        self.store = Store(self.url)
        self.store.init()
        self.account = self.store.create_caller()
        self.cookie = self.store.create_session(self.account)
        self.call = self.store.new_call(self.account["id"])
        self.reference = next(r["reference"] for r in self.store.list(self.account["id"]) if r["cancellation"]["refundable"])
        self.turn_id = None
        self.turn_text = self.agent_text = None
        JUDGED.clear()

    def tearDown(self):
        telemetry.drain()
        self.store.close()
        prepare_database.cache_clear()
        self.env.stop()
        self.temp.cleanup()

    def event(self, event):
        return process_event(self.store, self.account["id"], digest(self.cookie), self.call, event, assess=fake_assess)

    def turn(self, text, proposal=None, id=None, agent="Agent read the quote or readback.", channel="voice", key=None):
        self.turn_id, self.turn_text, self.agent_text, self.turn_proposal = id or str(uuid4()), text, agent, proposal
        return self.event({"kind": "turn", "turnId": self.turn_id, "text": text, "proposalId": proposal, "agentText": agent,
                           "channel": channel, "keypadKey": key})

    def keypad(self, key, proposal):
        return self.turn(f"Keypad {key}", proposal, channel="keypad", key=key)

    def climb_to_keypad(self, p):
        for _ in range(3):
            self.turn("Mmm, I suppose", p["id"])
            result = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertEqual(result["approvalStep"], "manual")
        self.assertIn("press 1. To cancel it, press 7", result["instruction"])

    def tool(self, name, args, request_id=None, turn_id=None):
        event = {"name": name, "args": args, "requestId": request_id or str(uuid4()), "turnId": turn_id or self.turn_id}
        if name in ("confirm_action", "resend_verification"):
            event.update(turnText=self.turn_text, agentText=self.agent_text)  # As the browser re-sends the judged turn.
        return self.event(event)

    def verified(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("Yeah, that's correct.")
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        code = self.store.inbox(digest(self.cookie))[0]["code"]
        self.turn(code)
        self.assertTrue(self.tool("verify_booking", {"code": code})["result"]["verified"])

    def quote_bag(self):
        self.verified()
        self.turn("add one bag")
        return self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "1"})["result"]

    def actions(self):
        return self.store.lookup(self.account["id"], self.reference)["actions"]

    def test_unclear_replies_escalate_to_yes_no_then_keypad_only(self):
        p = self.quote_bag()
        steps = []
        for _ in range(3):
            self.turn("Mmm, I suppose", p["id"])
            steps.append(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["approvalStep"])
        self.assertEqual(steps, ["natural", "closed", "manual"])
        self.turn("yes", p["id"])
        self.assertEqual(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["approvalStep"], "manual")
        self.assertEqual(self.actions(), [], "speech no longer approves at the keypad step")
        self.keypad("1", p["id"])
        done = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertTrue(done["done"])
        self.assertEqual(done["receipt"]["approval"]["channel"], "keypad")
        self.assertEqual(len(self.actions()), 1)

    def test_keypad_seven_cancels_and_other_keys_are_refused(self):
        p = self.quote_bag()
        self.climb_to_keypad(p)
        with self.assertRaisesRegex(ValueError, "keypad is only used"):
            self.keypad("2", p["id"])
        cancelled = self.keypad("7", p["id"])["result"]["approval"]
        self.assertTrue(cancelled["declined"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["requiresNewProposal"])
        with self.assertRaisesRegex(ValueError, "keypad is only used"):
            self.keypad("1", p["id"])  # A cancelled quote can never be confirmed afterwards.
        self.assertEqual(self.actions(), [])

    def test_spoken_no_cancels_at_keypad_step_but_spoken_yes_cannot_approve(self):
        p = self.quote_bag()
        self.climb_to_keypad(p)
        self.turn("yes", p["id"])
        self.assertEqual(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["approvalStep"], "manual")
        self.assertEqual(JUDGED[-1], ("change", "yes", "closed"))
        self.assertTrue(self.turn("no", p["id"])["result"]["approval"]["declined"])
        with self.assertRaisesRegex(ValueError, "keypad is only used"):
            self.keypad("1", p["id"])
        self.assertEqual(self.actions(), [])

    def test_withdrawn_quote_disappears_and_cannot_be_confirmed(self):
        p = self.quote_bag()
        self.turn("Actually, never mind.", p["id"])
        out = self.tool("withdraw_quote", {"proposalId": p["id"]})
        self.assertTrue(out["result"]["withdrawn"])
        self.assertEqual(out["state"]["proposals"][-1]["status"], "superseded")
        self.assertIn("Quote withdrawn", out["state"]["interactionNotes"][-1]["text"])
        self.turn("yes", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["requiresNewProposal"])
        self.assertFalse(self.tool("withdraw_quote", {"proposalId": p["id"]})["result"]["withdrawn"])
        self.assertEqual(self.actions(), [])

    def test_abandoned_call_is_not_renewed(self):
        with TestClient(app) as client:
            headers = {"origin": "http://testserver"}
            client.post("/api/start", headers=headers, json={})
            caller = self.store.authenticate(client.cookies["session"])
            call = self.store.new_call(caller["id"])
            with patch("app.genai.Client") as provider, patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
                provider.return_value.__enter__.return_value.auth_tokens.create.return_value.name = "auth_tokens/t"
                self.assertEqual(client.post("/api/token", headers=headers, json={"callId": call}).status_code, 200)
                with patch("app.now_ms", return_value=now_ms() + 11 * 60_000):
                    refused = client.post("/api/token", headers=headers, json={"callId": call})
                self.assertEqual(refused.status_code, 409)
                self.assertTrue(refused.json()["ended"])
                self.assertFalse(refused.json()["retryable"])
            saved = json.loads(self.store.rows("SELECT data FROM calls WHERE id=?", call)[0]["data"])
            self.assertTrue(saved["ended"])

    def test_closed_question_accepts_plain_yes_and_records_judges(self):
        p = self.quote_bag()
        for _ in range(2):
            self.turn("Mmm, I suppose", p["id"])
            self.tool("confirm_action", {"proposalId": p["id"]})
        self.turn("yes", p["id"])
        done = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertTrue(done["done"])
        self.assertEqual(done["receipt"]["approval"]["step"], "closed")
        self.assertEqual(JUDGED[-1], ("change", "yes", "closed"))
        self.assertEqual(done["receipt"]["approval"]["judges"][0]["evidence"], "yes")

    def test_keypad_needs_keypad_step_and_each_turn_is_judged_once(self):
        p = self.quote_bag()
        for key in ("1", "7"):
            with self.assertRaisesRegex(ValueError, "keypad is only used"):
                self.keypad(key, p["id"])
        self.turn("Mmm, I suppose", p["id"])
        JUDGED.clear()
        for _ in range(3):  # The agent retries on the same caller turn.
            self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["notConfirmed"])
        self.assertEqual(len(JUDGED), 1)
        with self.store.call(self.account["id"], self.call) as data:
            self.assertEqual(data["state"]["proposals"][-1]["approval"]["unclear"], 1, "retries cannot climb the ladder")

    def test_unheard_quote_and_tampered_transcript_never_approve(self):
        p = self.quote_bag()
        self.turn("yes", p["id"], agent="")
        unheard = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertIn("complete quote", unheard["instruction"])
        self.turn("yes", p["id"])
        forged = {"name": "confirm_action", "args": {"proposalId": p["id"]}, "requestId": str(uuid4()), "turnId": self.turn_id,
                  "turnText": "yes", "agentText": "A different quote.",
                  "assessment": {"kind": "change", "proposalId": p["id"], "mode": "natural", "decision": "approve", "judges": []}}
        self.assertTrue(self.event(forged)["result"]["notConfirmed"])
        self.assertEqual(self.actions(), [])
        with self.assertRaises(ValueError):
            validate_tool("confirm_action", {"proposalId": p["id"], "assessment": "approve"})

    def test_spaced_code_and_fare_labels(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("Yes, that's correct.")
        code = self.store.inbox(digest(self.cookie))[0]["code"]
        spoken = " ".join(code[:3]) + "-" + " ".join(code[3:])
        self.turn(spoken)
        self.assertTrue(self.tool("verify_booking", {"code": spoken})["result"]["verified"])
        labels = [trip_label(b) for b in self.store.list(self.account["id"])]
        self.assertTrue(any(l.endswith("· refundable") or "· refundable ·" in l for l in labels))
        self.assertTrue(any("nonrefundable" in l for l in labels))

    def test_general_policy_is_available_before_booking_verification(self):
        self.turn("Hey, my name is Michael Jackson.")
        premature = self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.assertTrue(premature["result"]["notConfirmed"])
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])
        policy = {"status": "snapshot", "facts": ["General baggage guidance."]}
        result = process_event(self.store, self.account["id"], digest(self.cookie), self.call,
                               {"name": "lookup_policy", "args": {"topic": "baggage"}, "requestId": str(uuid4())}, policy=policy)
        self.assertEqual(result["result"], policy)
        for name, args in (("get_case", {}),
                           ("get_options", {"kind": "bags", "segment": "", "date": "", "route": ""}),
                           ("propose_action", {"action": "add_bags", "segment": "", "value": "1"}),
                           ("confirm_action", {"proposalId": "unverified"})):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Verify"):
                self.tool(name, args)

    def test_rolling_rate_limit_blocks_boundary_bursts_and_concurrent_overflow(self):
        with patch("database.now_ms", return_value=59_000):
            for _ in range(100):
                self.store.limit("burst", 100, 60_000)
        with patch("database.now_ms", return_value=60_000):
            for _ in range(100):
                with self.assertRaises(RateLimitExceeded) as blocked:
                    self.store.limit("burst", 100, 60_000)
                self.assertEqual(blocked.exception.retry_after, 59)
        with patch("database.now_ms", return_value=119_000):
            self.store.limit("burst", 100, 60_000)

        def concurrent_request(_):
            store = Store(self.url)
            try:
                store.limit("shared", 5, 60_000)
                return True
            except RateLimitExceeded:
                return False
            finally:
                store.close()

        with patch("database.now_ms", return_value=200_000), ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(concurrent_request, range(20))), 5)

    def test_pending_identity_continues_after_yes_and_correction_invalidates_it(self):
        args = {"reference": self.reference, "name": "Michael Jackson"}
        for _ in range(4):
            pending = self.tool("request_verification", args)["result"]
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM limit_events")[0]["n"], 0)
        self.assertTrue(pending["notConfirmed"])
        self.assertIn("The passenger name is Michael Jackson.", pending["readback"])
        self.assertEqual(pending["readback"], identity_readback(self.reference, args["name"]))
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])
        approval = self.turn("Yes, that's correct.")
        self.assertTrue(approval["result"]["verificationResult"]["sent"])
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1)
        self.assertEqual(self.turn("Yes, that's correct.", id=self.turn_id), approval)
        sent = self.tool("request_verification", args)
        self.assertTrue(sent["result"]["sent"])
        self.assertTrue(self.tool("request_verification", args)["result"]["sent"])
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1)
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM limit_events")[0]["n"], 1)
        self.call = self.store.new_call(self.account["id"])
        self.tool("request_verification", args)
        self.turn("No, the last name is different.")
        self.assertNotIn("continueVerification", self.turn("Yes, correct.")["result"])
        self.assertTrue(self.tool("request_verification", args)["result"]["notConfirmed"])

    def test_phonetic_clarification_is_per_field_and_still_requires_confirmation(self):
        self.assertEqual(identity_readback("R2R82A", "Michael Jackson"), "The confirmation number is R, two, R, eight, two, A. The passenger name is Michael Jackson. Is that correct?")
        for mode in ("none", "name", "reference", "both"):
            readback = identity_readback("R2R82A", "Michael Jackson", mode)
            self.assertEqual("Mike, India" in readback, mode in ("name", "both"))
            self.assertEqual("Romeo" in readback, mode in ("reference", "both"))
        args = {"reference": self.reference, "name": "Michael Jackson"}
        validate_tool("request_verification", args)
        validate_tool("request_verification", dict(args, phonetic="name"))
        for bad in (dict(args, phonetic=True), dict(args, phonetic="always"), {"name": args["name"]}, dict(args, confirmed="yes")):
            with self.assertRaises(ValueError):
                validate_tool("request_verification", bad)
        self.tool("request_verification", args)
        self.turn("No, you misheard my name.")
        pending = self.tool("request_verification", dict(args, phonetic="name"))["result"]
        self.assertIn("Mike, India", pending["readback"])
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])
        result = self.turn("Yes, that's correct.")["result"]["verificationResult"]
        self.assertTrue(result["sent"])
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1)

    def test_approval_send_failure_rolls_back_and_retry_sends_once(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        turn_id = str(uuid4())
        with patch.object(self.store, "save_tool_receipt", side_effect=DatabaseFailure()):
            with self.assertRaises(DatabaseFailure):
                self.turn("That's correct.", id=turn_id)
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])
        result = self.turn("That's correct.", id=turn_id)
        self.assertTrue(result["result"]["verificationResult"]["sent"])
        self.assertEqual(self.turn("That's correct.", id=turn_id), result)
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 1)

    def test_expired_identity_returns_recovery_instead_of_silence(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        with self.store.call(self.account["id"], self.call) as data:
            data["workflow"]["pendingIdentity"]["createdAt"] -= 61_000
        result = self.turn("That's correct.")["result"]["verificationResult"]
        self.assertTrue(result["notConfirmed"])
        self.assertIn("fresh readback", result["instruction"])
        self.assertEqual(self.store.inbox(digest(self.cookie)), [])

    def test_verification_resend_and_incomplete_codes(self):
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.turn("Yes, correct.")
        self.tool("request_verification", {"reference": self.reference, "name": "Michael Jackson"})
        self.assertTrue(self.tool("verify_booking", {"code": "123456"})["result"]["notConfirmed"])
        for partial in ("27415", "2474215", "27x217", ""):
            self.turn(partial or "I could not read it")
            self.assertTrue(self.tool("verify_booking", {"code": partial})["result"]["invalidFormat"])
        self.assertEqual(self.store.rows("SELECT attempts FROM verification")[0]["attempts"], 0)
        self.turn("Please send a new code.")
        request = str(uuid4())
        result = self.tool("resend_verification", {}, request)
        self.assertTrue(result["result"]["sent"])
        self.assertTrue(result["state"]["verificationPending"])
        self.assertEqual(self.tool("resend_verification", {}, request)["result"], result["result"])
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 2)
        self.assertTrue(self.tool("resend_verification", {})["result"]["sent"])
        self.assertEqual(len(self.store.inbox(digest(self.cookie))), 2)
        code = self.store.inbox(digest(self.cookie))[0]["code"]
        self.turn(code)
        self.assertTrue(self.tool("verify_booking", {"code": code})["result"]["verified"])
        with self.assertRaises(ValueError):
            self.tool("resend_verification", {})
        with self.assertRaisesRegex(ValueError, "six"):
            validate_tool("request_verification", {"reference": "3QC5YYU", "name": "Michael Jackson"})

    def test_interaction_notes_append_survive_verification_and_replay(self):
        request_id = str(uuid4())
        args = {"notes": "Customer wants to postpone the outbound flight."}
        first = self.tool("save_notes", args, request_id)["state"]["interactionNotes"]
        self.assertEqual(len(first), 2)  # Session opened plus caller goal.
        self.assertEqual(first[-1]["text"], args["notes"])
        self.assertIsInstance(first[-1]["timestamp"], int)
        self.assertEqual(self.tool("save_notes", args, request_id)["state"]["interactionNotes"], first)
        self.verified()
        updated = self.tool("save_notes", {"notes": "Customer prefers an evening flight."})["state"]["interactionNotes"]
        self.assertEqual(updated[:2], first)
        self.assertIn("Verified booking", updated[-2]["text"])
        self.assertEqual(len(updated), 5)
        self.assertEqual(sorted(n["timestamp"] for n in updated), [n["timestamp"] for n in updated])
        code = self.store.inbox(digest(self.cookie))[0]["code"]
        self.assertNotIn(code, json.dumps(updated))
        with patch.object(self.store, "save_tool_receipt", side_effect=DatabaseFailure()):
            with self.assertRaises(DatabaseFailure):
                self.tool("save_notes", {"notes": "Must roll back."})
        with self.store.call(self.account["id"], self.call) as data:
            self.assertEqual(data["state"]["interactionNotes"], updated)
        self.turn("Please add one checked bag.")
        quoted = self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "1"})
        self.assertIn("Awaiting customer approval", quoted["state"]["interactionNotes"][-1]["text"])
        proposal = quoted["result"]["id"]
        self.turn("Yes, go ahead.", proposal)
        completed = self.tool("confirm_action", {"proposalId": proposal})
        self.assertTrue(completed["state"]["interactionNotes"][-1]["text"].startswith("Completed:"))

    def test_handoff_brief_is_saved_atomically_for_representative_review(self):
        self.verified()
        self.tool("save_notes", {"notes": "Caller wants help changing checked bags."})
        current_bags = self.store.lookup(self.account["id"], self.reference)["reservation"]["checkedBags"]
        self.turn("I want to change my checked bags.")
        proposal = self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "0" if current_bags else "1"})["result"]
        args = {"reason": "Caller requested a representative.",
                "notes": "Caller wants help changing checked bags. Booking verified. A quote is pending but was not approved; representative should review options."}
        request_id = str(uuid4())
        with patch.object(self.store, "save_tool_receipt", side_effect=DatabaseFailure()):
            with self.assertRaises(DatabaseFailure):
                self.tool("transfer_to_representative", args, request_id)
        saved = json.loads(self.store.rows("SELECT data FROM calls WHERE id=?", self.call)[0]["data"])
        self.assertFalse(saved["ended"])
        self.assertNotIn("handoff", saved)

        result = self.tool("transfer_to_representative", args, request_id)["result"]
        brief = result["brief"]
        self.assertTrue(result["handoff"])
        self.assertFalse(result["actualTransfer"])
        self.assertEqual(brief["bookingReference"], self.reference)
        self.assertTrue(brief["bookingVerified"])
        self.assertEqual(brief["notes"], args["notes"])
        self.assertEqual(brief["unapprovedProposal"], proposal["summary"])
        self.assertEqual(brief["completedActions"], [])
        saved = json.loads(self.store.rows("SELECT data FROM calls WHERE id=?", self.call)[0]["data"])
        self.assertTrue(saved["ended"])
        self.assertEqual(saved["handoff"], brief)
        self.assertEqual(saved["state"]["notes"], args["notes"])
        self.assertEqual(saved["state"]["proposals"][-1]["status"], "superseded")
        with self.assertRaisesRegex(ValueError, "ended"):
            self.tool("get_case", {})

    def test_all_booking_actions_and_consistent_quotes(self):
        state = {"version": 0, "notes": "", "proposals": [], "actions": [], "reservation": booking()}
        def commit(action, segment, value):
            p = propose(state, action, segment, value)
            return decide(state, p["id"], True)
        options = get_options(state["reservation"], "flights", "1", "", "")["options"]
        self.assertEqual(options, get_options(state["reservation"], "flights", "1", "", "")["options"])
        commit("change_flight", "1", options[0]["id"])
        seats = get_options(state["reservation"], "seats", "1", "", "")["sections"][1]["available"]
        commit("change_seat", "1", seats[0].split()[0])
        upgrade = get_options(state["reservation"], "upgrades", "1", "", "")["segments"][0]["options"][0]
        self.assertEqual(upgrade["priceUSD"], plan(state["reservation"], "upgrade", "1", upgrade["value"])["amountUSD"])
        commit("upgrade", "1", "first")
        for option in get_options(state["reservation"], "flights", "1", "", "")["options"]:
            self.assertEqual(option["fareDifferenceUSD"], plan(state["reservation"], "change_flight", "1", option["id"])["amountUSD"])
        commit("special_meal", "all", "VGML")
        commit("add_bags", "", "2")
        commit("special_assistance", "", "WCHR")
        commit("special_assistance", "", "remove:WCHR")
        commit("pet", "", "dog")
        commit("loyalty", "", "ab 123-456")
        r = state["reservation"]
        self.assertEqual(r["loyaltyNumber"], "AB123456")
        if r["creditUSD"]:
            with self.assertRaisesRegex(ValueError, "valuation"):
                commit("cancel_and_refund", "", "")
        state["reservation"] = booking()
        total = state["reservation"]["paidUSD"]
        commit("cancel_and_refund", "", "")
        self.assertEqual(state["reservation"]["refundRequestedUSD"], total)
        self.assertEqual(state["version"], len(state["actions"]))
        with self.assertRaisesRegex(ValueError, "already"):
            propose(state, "cancel_and_refund", "", "")

    def test_stale_yes_requires_new_proposal_specific_turn(self):
        self.verified()
        self.turn("yes")
        stale_yes = self.turn_id
        p = self.tool("propose_action", {"action": "cancel_and_refund", "segment": "", "value": ""})["result"]
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]}, turn_id=stale_yes)["result"]["notConfirmed"])
        self.turn("yes")  # A new yes without the proposal binding is also insufficient.
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["notConfirmed"])
        self.turn("yes, go ahead", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["done"])
        self.assertEqual(len(self.store.lookup(self.account["id"], self.reference)["actions"]), 1)

    def test_receipts_survive_restart_and_lost_response(self):
        self.verified()
        self.turn("please cancel and refund")
        p = self.tool("propose_action", {"action": "cancel_and_refund", "segment": "", "value": ""})["result"]
        self.turn("yes", p["id"])
        request_id = str(uuid4())
        output = self.tool("confirm_action", {"proposalId": p["id"]}, request_id)
        self.store.close()
        self.store = Store(self.url)
        # Simulate in-memory receipt cache eviction and a process restart.
        with self.store.call(self.account["id"], self.call) as data:
            data["results"] = {}
        self.assertEqual(self.tool("confirm_action", {"proposalId": p["id"]}, request_id)["result"], output["result"])
        self.assertEqual(len(self.store.lookup(self.account["id"], self.reference)["actions"]), 1)
        with self.assertRaisesRegex(ValueError, "reused"):
            self.tool("get_case", {}, request_id)

    def test_refund_confirmation_after_neutral_reply(self):
        self.verified()
        self.turn("cancel and refund please")
        p = self.tool("propose_action", {"action": "cancel_and_refund", "segment": "", "value": ""})["result"]
        self.turn("That's great. Thank you.", p["id"])
        rejected = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertTrue(rejected["notConfirmed"])
        self.assertEqual(self.tool("get_case", {})["result"]["proposals"][-1]["status"], "pending")
        self.turn("Yes, please go ahead.", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["done"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["done"])
        self.assertEqual(len(self.store.lookup(self.account["id"], self.reference)["actions"]), 1)

    def test_conversational_approval_commits_flight_change_once(self):
        self.verified()
        for approval in ("Yeah, that would be great.", "Yes, please proceed."):
            options = self.tool("get_options", {"kind": "flights", "segment": "1", "date": "", "route": ""})["result"]["options"]
            self.turn("Please change to that flight")
            p = self.tool("propose_action", {"action": "change_flight", "segment": "1", "value": options[0]["id"]})["result"]
            self.turn(approval, p["id"])
            request = str(uuid4())
            result = self.tool("confirm_action", {"proposalId": p["id"]}, request)["result"]
            self.assertTrue(result["done"])
            self.assertEqual(self.tool("confirm_action", {"proposalId": p["id"]}, request)["result"], result)
        self.assertEqual(len(self.store.lookup(self.account["id"], self.reference)["actions"]), 2)

    def test_concurrent_retry_and_rejected_request_replay(self):
        self.verified()
        self.turn("add one bag")
        p = self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "1"})["result"]
        rejected_id = str(uuid4())
        rejected = self.tool("confirm_action", {"proposalId": p["id"]}, rejected_id)
        self.turn("yes", p["id"])
        self.assertEqual(self.tool("confirm_action", {"proposalId": p["id"]}, rejected_id)["result"], rejected["result"])
        event = {"name": "confirm_action", "args": {"proposalId": p["id"]}, "requestId": str(uuid4()), "turnId": self.turn_id,
                 "turnText": self.turn_text, "agentText": self.agent_text}
        def retry(_):
            store = Store(self.url)
            try:
                return process_event(store, self.account["id"], digest(self.cookie), self.call, event, assess=fake_assess)["result"]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(retry, range(4)))
        self.assertTrue(results[0]["done"])
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(self.store.lookup(self.account["id"], self.reference)["actions"]), 1)

    def test_interruption_rejection_expiry_and_rollback(self):
        self.verified()
        self.turn("add a bag")
        request_id = str(uuid4())
        p = self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "1"}, request_id)["result"]
        self.turn("Yes, but wait", p["id"])
        held = self.tool("confirm_action", {"proposalId": p["id"]})["result"]
        self.assertTrue(held["notConfirmed"])
        self.assertIn("question", held["instruction"])  # A hedge keeps the quote open; it neither approves nor cancels.
        self.turn("no", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["declined"])
        self.turn("yes", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["requiresNewProposal"])
        self.turn("add a bag")
        p = self.tool("propose_action", {"action": "add_bags", "segment": "", "value": "1"})["result"]
        self.turn("yes", p["id"])
        real_commit = self.store.commit
        def fail_after_write(*args):
            real_commit(*args)
            raise DatabaseFailure()
        before = self.store.lookup(self.account["id"], self.reference)
        failed_id = str(uuid4())
        with patch.object(self.store, "commit", side_effect=fail_after_write), self.assertRaises(DatabaseFailure):
            self.tool("confirm_action", {"proposalId": p["id"]}, failed_id)
        self.assertEqual(self.store.lookup(self.account["id"], self.reference), before)
        self.assertIsNone(self.store.tool_receipt(self.account["id"], self.call, failed_id))
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]}, failed_id)["result"]["done"])
        self.turn("cancel")
        p = self.tool("propose_action", {"action": "cancel_and_refund", "segment": "", "value": ""})["result"]
        with self.store.call(self.account["id"], self.call) as data:
            data["state"]["proposals"][-1]["expiresAt"] = 0
        self.turn("yes", p["id"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": p["id"]})["result"]["requiresNewProposal"])
        self.assertTrue(self.tool("confirm_action", {"proposalId": "unknown"})["result"]["requiresNewProposal"])

    def test_otp_attempts_replay_privacy_and_isolation(self):
        other = self.store.create_caller()
        with self.assertRaisesRegex(ValueError, "No reservation"):
            self.store.lookup(other["id"], self.reference)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            with self.store.call(other["id"], self.call):
                pass
        with self.assertRaisesRegex(ValueError, "Verify"):
            self.tool("get_case", {})
        self.tool("request_verification", {"reference": self.reference, "name": "MICHAEL  jackson"})
        self.turn("yes")
        self.tool("request_verification", {"reference": self.reference, "name": "MICHAEL  jackson"})
        code = self.store.inbox(digest(self.cookie))[0]["code"]
        self.turn("000000")
        request_id = str(uuid4())
        first = self.tool("verify_booking", {"code": "000000"}, request_id)
        self.assertEqual(self.tool("verify_booking", {"code": "000000"}, request_id)["result"], first["result"])
        self.assertEqual(self.store.rows("SELECT attempts FROM verification")[0]["attempts"], 1)
        receipts = self.store.rows("SELECT fingerprint,result FROM tool_receipts")
        self.assertNotIn(code, json.dumps(receipts))
        self.assertNotIn("000000", json.dumps(receipts))
        with self.assertRaisesRegex(ValueError, "reused"):
            self.tool("verify_booking", {"code": code}, request_id)
        self.turn(code)
        self.assertTrue(self.tool("verify_booking", {"code": code})["result"]["verified"])
        self.assertEqual(self.store.inbox(digest("other")), [])

    def test_turn_replay_legacy_state_and_deadline(self):
        id = str(uuid4())
        first = self.turn("yes", id=id)
        self.assertEqual(first["result"], self.turn("yes", id=id)["result"])
        with self.assertRaisesRegex(ValueError, "reused"):
            self.turn("no", id=id)
        with self.store.call(self.account["id"], self.call) as data:
            del data["workflow"]
            data["state"]["proposals"] = [{"status": "pending"}]
        with self.store.call(self.account["id"], self.call) as data:
            self.assertEqual(data["state"]["proposals"][0]["status"], "superseded")
        before = self.store.lookup(self.account["id"], self.reference)
        with self.assertRaises(TimeoutError):
            with self.store.call(self.account["id"], self.call):
                self.store.execute("UPDATE bookings SET version=99 WHERE reference=?", self.reference)
                self.store.tx_deadline = time.monotonic() - 1
        self.assertEqual(self.store.lookup(self.account["id"], self.reference), before)

    def test_http_contract_link_gate_receipts_and_no_secrets(self):
        with TestClient(app) as client:
            headers = {"origin": "http://testserver"}
            def post(route, body):
                return client.post("/api/" + route, headers=headers, json=body)
            self.assertEqual(post("start", {}).status_code, 200)
            token = client.cookies["session"]
            caller = self.store.authenticate(token)
            call = self.store.new_call(caller["id"])
            self.assertEqual(post("start", {}).json()["caller"]["name"], "Michael Jackson")
            self.assertEqual(post("token", {}).status_code, 503)
            with patch.object(Store, "limit", side_effect=RateLimitExceeded(42)):
                limited = post("token", {})
                self.assertEqual(limited.status_code, 429)
                self.assertEqual(limited.headers["retry-after"], "42")
                self.assertFalse(limited.json()["retryable"])
            with patch("app.genai.Client") as provider, patch.dict(os.environ, {"GEMINI_API_KEY": "long-lived-test-secret"}):
                provider.return_value.__enter__.return_value.auth_tokens.create.return_value.name = "auth_tokens/temporary"
                issued = post("token", {"callId": call})
                self.assertEqual(issued.status_code, 200)
                self.assertNotIn("long-lived-test-secret", issued.text)
                config = provider.return_value.__enter__.return_value.auth_tokens.create.call_args.kwargs["config"]
                self.assertEqual(config["uses"], 1)
                self.assertEqual(config["live_connect_constraints"]["config"], live_config())
            self.assertEqual(post("turn", {"callId": call, "turnId": str(uuid4()), "text": "yes"}).status_code, 200)
            tool = {"callId": call, "requestId": str(uuid4()), "name": "save_notes", "args": {"notes": "Caller wants a flight change."}}
            response = post("tool", tool)
            self.assertEqual(response.json()["result"], {"saved": True})
            self.assertEqual(post("tool-status", {"callId": call, "requestId": tool["requestId"]}).json()["status"], "completed")
            post("cancel", {"callId": call, "ids": [tool["requestId"]]})
            self.assertEqual(post("tool-status", {"callId": call, "requestId": tool["requestId"]}).json()["status"], "completed", "committed result wins over cancellation")
            cancelled_id = str(uuid4())
            post("cancel", {"callId": call, "ids": [cancelled_id]})
            self.assertTrue(post("tool", dict(tool, requestId=cancelled_id)).json()["result"]["cancelled"])
            with self.store.call(caller["id"], call) as data:
                data["cancelled"] = []
            self.assertTrue(post("tool", dict(tool, requestId=cancelled_id)).json()["result"]["cancelled"], "cancellation survives bounded cache eviction")
            self.assertEqual(post("tool-status", {"callId": call, "requestId": cancelled_id}).json()["status"], "cancelled")
            with patch("app.process_event", side_effect=DatabaseFailure()):
                failure = post("tool", dict(tool, requestId=str(uuid4())))
                self.assertEqual(failure.status_code, 503)
                self.assertTrue(failure.json()["retryable"])
            with patch("app.process_event", side_effect=TimeoutError()):
                self.assertEqual(post("tool", dict(tool, requestId=str(uuid4()))).status_code, 504)
            self.assertEqual(client.post("/api/start", headers={"origin": "https://attacker.invalid"}, json={}).status_code, 403)
            self.assertEqual(post("start", {"bad": "credential-to-hide"}).status_code, 400)
            self.assertNotIn("credential-to-hide", post("start", {"bad": "credential-to-hide"}).text)
            self.assertEqual(client.post("/api/start", headers=dict(headers, **{"content-type": "application/json"}), content='{"key":"' + 'a' * 17000 + '"}').status_code, 413)
            share_id = str(uuid4())
            with patch.dict(os.environ, {"ACCESS_KEY": share_id}):
                with TestClient(app) as visitor:
                    self.assertEqual(visitor.get("/").status_code, 403)
                    self.assertEqual(visitor.get("/style.css").status_code, 403)
                    self.assertEqual(visitor.post("/api/token", headers=headers, json={}).status_code, 403)
                    self.assertEqual(visitor.get("/?k=wrong", follow_redirects=False).status_code, 403)
                    admitted = visitor.get("/?k=" + share_id, follow_redirects=False)
                    self.assertEqual(admitted.status_code, 303)
                    self.assertEqual(admitted.headers["location"], "/")
                    self.assertIn("httponly", admitted.headers["set-cookie"].lower())
                    self.assertNotEqual(visitor.get("/").status_code, 403)
                self.assertEqual(post("start", {}).status_code, 403)
                self.assertEqual(post("start", {"key": share_id}).status_code, 200)
                self.assertEqual(post("start", {}).status_code, 200)
            with patch.dict(os.environ, {"APP_ORIGIN": "https://testserver", "ACCESS_KEY": ""}):
                self.assertEqual(client.get("/").status_code, 503)
            handoff = dict(tool, requestId=str(uuid4()), name="transfer_to_representative", args={"reason": "Caller requested a person.", "notes": "Caller asked for a person. No booking was verified; representative should ask how to help."})
            first = post("tool", handoff).json()["result"]
            self.assertTrue(first["handoff"])
            self.assertFalse(first["brief"]["bookingVerified"])
            self.assertEqual(post("tool", handoff).json()["result"], first)
            self.assertEqual(client.get("/.env").status_code, 404)

    def test_policy_fallback_validation_and_natural_readback(self):
        for tool in live_config()["tools"][0]["functionDeclarations"]:
            self.assertEqual(tool["behavior"], "BLOCKING" if tool["name"] == "transfer_to_representative" else "NON_BLOCKING")
        with self.assertRaises(ValueError):
            validate_tool("lookup_policy", {"topic": "http://internal"})
        with self.assertRaises(ValueError):
            validate_tool("propose_action", {"action": "delete_everything", "segment": "", "value": ""})
        with self.assertRaises(ValueError):
            validate_tool("transfer_to_representative", {"reason": "Caller requested a person."})
        with self.assertRaisesRegex(ValueError, "verification codes"):
            validate_tool("transfer_to_representative", {"reason": "Caller requested a person.", "notes": "Caller read code 123456."})
        policy = lookup_policy("baggage")
        self.assertEqual(policy["status"], "snapshot")
        self.assertTrue(policy["facts"])

    def test_remote_transport_timeout_and_parameter_binding(self):
        seen = []
        def provider(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"baton": "private-baton", "base_url": None, "results": [{"type": "ok", "response": {"result": {"cols": [{"name": "x"}], "rows": [[{"type": "integer", "value": "3"}]], "affected_row_count": 1}}}]})
        db = HttpDatabase("libsql://example.turso.io", "secret", httpx.Client(transport=httpx.MockTransport(provider)))
        try:
            self.assertEqual(db.execute("SELECT ? AS x", (3,)).fetchall(), [(3,)])
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(seen[1]["baton"], "private-baton")
            self.assertEqual(seen[0]["requests"][0]["stmt"]["args"], [{"type": "integer", "value": "3"}])
            db.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError):
                db.execute("COMMIT")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
