"""Approval judgments: verified output, fail-closed behaviour and the cost cascade (provider mocked)."""
import unittest
from unittest.mock import patch

import consent


def reply(decision, heard=True, evidence="yeah go for it"):
    return {"decision": decision, "heardQuote": heard, "evidence": evidence}


class ConsentTests(unittest.TestCase):
    def judged(self, outputs, kind="change", caller="Yeah go for it", agent="That's fifty-nine dollars. Go ahead?"):
        calls = []

        def model(name, system, prompt):
            calls.append(name)
            result = outputs[len(calls) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(consent, "call_model", side_effect=model):
            return consent.assess(kind, "Move to seat 12A for $59.", agent, caller), calls

    def test_change_needs_two_different_judges_and_verifier_runs_only_after_approval(self):
        result, calls = self.judged([reply("approve"), reply("approve")])
        self.assertEqual(result["decision"], "approve")
        self.assertEqual(calls, [consent.JUDGE_MODEL, consent.VERIFIER_MODEL])
        self.assertNotEqual(consent.JUDGE_MODEL, consent.VERIFIER_MODEL)
        for first in ("decline", "question", "unclear"):
            result, calls = self.judged([reply(first, evidence="Yeah go for it" if first != "unclear" else "")])
            self.assertEqual((result["decision"], len(calls)), (first, 1), "non-approvals cost one call")

    def test_disagreement_and_unheard_quotes_never_approve(self):
        self.assertEqual(self.judged([reply("approve"), reply("unclear", evidence="")])[0]["decision"], "unclear")
        self.assertEqual(self.judged([reply("approve"), reply("decline")])[0]["decision"], "unclear")
        result, calls = self.judged([reply("approve", heard=False)])
        self.assertEqual((result["decision"], len(calls)), ("notHeard", 1))
        self.assertEqual(self.judged([reply("approve"), reply("approve", heard=False)])[0]["decision"], "notHeard")

    def test_invented_or_malformed_output_and_provider_failure_fail_closed(self):
        invented = reply("approve", evidence="yes, charge my card")
        self.assertEqual(self.judged([invented])[0]["decision"], "unclear", "evidence must be the caller's own words")
        for bad in ({"decision": "approved"}, {"decision": "approve", "heardQuote": "yes", "evidence": "go"}, None, []):
            self.assertEqual(self.judged([bad])[0]["decision"], "unclear")
        self.assertEqual(self.judged([TimeoutError()])[0]["decision"], "unclear")
        self.assertEqual(self.judged([reply("approve"), RuntimeError("quota")])[0]["decision"], "unclear")

    def test_evidence_match_ignores_case_and_spacing_only(self):
        self.assertEqual(self.judged([reply("approve", evidence="YEAH  go for it"), reply("approve")])[0]["decision"], "approve")

    def test_identity_and_resend_use_one_judge(self):
        result, calls = self.judged([reply("approve", evidence="That's right")], kind="identity", caller="That's right")
        self.assertEqual((result["decision"], len(calls)), ("approve", 1))
        result, calls = self.judged([reply("approve", heard=False, evidence="send me another code")], kind="resend", caller="Send me another code")
        self.assertEqual((result["decision"], len(calls)), ("approve", 1))

    def test_unconfigured_provider_is_unclear_not_an_error(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            self.assertEqual(consent.assess("change", "Quote", "Quote. Go ahead?", "Yes")["decision"], "unclear")


if __name__ == "__main__":
    unittest.main()
