"""Inventory evidence, not conversational claims, authorizes cancellation quotes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from tools import decide, generate_booking, plan, propose


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        self.ticket = generate_booking("Demo Caller", route=("DEN", "SFO"), refundable=False)
        for segment in self.ticket["segments"]:
            segment["date"] = "2026-11-01"
        self.ticket["cancellation"].update(purchasedAt="2026-10-01T12:00:00+00:00", originalDepartureAt="2026-11-01T12:00:00+00:00", creditExpiresOn="2027-10-01")
        self.clock = patch("tools.datetime", wraps=datetime)
        self.clock.start().now.return_value = self.now
        self.addCleanup(self.clock.stop)

    def quote(self, ticket=None, action="cancel_and_refund"):
        return plan(ticket or self.ticket, action, "", "The caller says they qualify")

    def test_nonrefundable_requires_evidence_and_credit_discloses_fee(self):
        with self.assertRaisesRegex(ValueError, "does not qualify"):
            self.quote()
        self.ticket["cancellation"]["creditFeeUSD"] = 50
        result = self.quote(action="cancel_for_credit")
        self.assertEqual(result["next"]["creditUSD"], self.ticket["paidUSD"] - 50)
        self.assertIn("2027-10-01", result["summary"])
        with self.assertRaisesRegex(ValueError, "valuation"):
            self.quote(result["next"])

    def test_24_hour_and_seven_day_boundaries(self):
        facts = self.ticket["cancellation"]
        facts.update(purchasedAt=(self.now-timedelta(hours=24)).isoformat(), originalDepartureAt=(self.now-timedelta(hours=24)+timedelta(days=7)).isoformat())
        self.assertIn("24-hour", self.quote()["summary"])
        facts["purchasedAt"] = (self.now-timedelta(hours=24, seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            self.quote()
        facts.update(purchasedAt=self.now.isoformat(), originalDepartureAt=(self.now+timedelta(days=7, seconds=-1)).isoformat())
        with self.assertRaises(ValueError):
            self.quote()

    def test_refundable_and_disrupted_paths(self):
        facts = self.ticket["cancellation"]
        facts["refundable"] = True
        self.assertIn("refundable fare", self.quote()["summary"])
        facts.update(refundable=False, qualifyingDisruption=True)
        self.assertIn("disruption", self.quote()["summary"])
        facts["alternativeAccepted"] = True
        with self.assertRaises(ValueError):
            self.quote()

    def test_missing_third_party_used_and_invalid_evidence_need_review(self):
        for update in ({"issuer": "Agency"}, {"purchasedDirect": False}, {"unused": False}, {"purchasedAt": "yesterday"}, {"alternativeAccepted": "false"}):
            with self.subTest(update=update):
                ticket = deepcopy(self.ticket)
                ticket["cancellation"].update(refundable=True, **update)
                with self.assertRaisesRegex(ValueError, "review"):
                    self.quote(ticket)
        del self.ticket["cancellation"]
        with self.assertRaises(ValueError):
            self.quote()

    def test_commit_rechecks_eligibility_and_credit_terms(self):
        state = {"reservation": self.ticket, "version": 0, "actions": [], "proposals": []}
        self.ticket["cancellation"]["refundable"] = True
        proposal = propose(state, "cancel_and_refund", "", "")
        self.ticket["cancellation"]["refundable"] = False
        with self.assertRaises(ValueError):
            decide(state, proposal["id"], True)
        self.assertEqual(state["actions"], [])
        proposal = propose(state, "cancel_for_credit", "", "")
        self.ticket["cancellation"]["creditFeeUSD"] = 50
        with self.assertRaisesRegex(ValueError, "changed"):
            decide(state, proposal["id"], True)
        self.assertEqual(state["reservation"]["status"], "confirmed")

    def test_same_day_is_review_not_guessed_departure_time(self):
        self.ticket["cancellation"]["refundable"] = True
        self.ticket["segments"][0]["date"] = self.now.date().isoformat()
        with self.assertRaises(ValueError):
            self.quote()
