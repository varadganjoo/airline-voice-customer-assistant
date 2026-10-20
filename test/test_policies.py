from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import patch

from tools import POLICIES, lookup_policy


class PolicyTests(TestCase):
    def test_every_topic_returns_reviewed_facts_without_network_or_external_links(self):
        for topic in POLICIES:
            with self.subTest(topic=topic):
                result = lookup_policy(topic)
                self.assertEqual(result["status"], "snapshot")
                self.assertTrue(result["facts"])
                self.assertNotIn("url", result)

    def test_concurrent_callers_receive_independent_policy_copies(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lookup_policy, ["refunds"] * 8))
        results[0]["facts"].clear()
        self.assertTrue(all(result["facts"] for result in results[1:]))
        self.assertTrue(lookup_policy("refunds")["facts"])

    def test_unknown_topic_is_rejected(self):
        with self.assertRaises(ValueError):
            lookup_policy("http://internal")

    def test_old_snapshot_fails_closed_and_boundary_is_inclusive(self):
        for age, expected in [(30, "snapshot"), (31, "unavailable")]:
            reviewed = (date.today() - timedelta(days=age)).isoformat()
            with patch.dict(POLICIES["refunds"], {"reviewed": reviewed}):
                result = lookup_policy("refunds")
            self.assertEqual(result["status"], expected)
            self.assertEqual(bool(result["facts"]), age <= 30)
