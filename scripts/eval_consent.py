"""Billable: run the real approval judges over the labelled cases and report safety, accuracy, latency and cost.

Usage: uv run --frozen --env-file .env python scripts/eval_consent.py [--judge MODEL] [--verifier MODEL] [--repeat N]
Exit code 1 if any reply labelled decline/hold is approved (a false approval).
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consent  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--judge", default=consent.JUDGE_MODEL)
parser.add_argument("--verifier", default=consent.VERIFIER_MODEL)
parser.add_argument("--repeat", type=int, default=1, help="Run each case N times to expose nondeterminism.")
parser.add_argument("--cases", default="consent_cases.json", help="File under test/, e.g. consent_holdout.json.")
options = parser.parse_args()
consent.JUDGE_MODEL, consent.VERIFIER_MODEL = options.judge, options.verifier

data = json.loads(Path(__file__).resolve().parents[1].joinpath("test", options.cases).read_text(encoding="utf-8"))
calls = []
real_call = consent.call_model


def counted(model, system, prompt):
    calls.append(model)
    return real_call(model, system, prompt)


consent.call_model = counted


def run(case):
    quote = data["quotes"].get(case.get("quote"), {})
    started = time.monotonic()
    result = consent.assess(case["kind"], case.get("subject") or quote["subject"], case.get("agent") or quote["agent"],
                            case["caller"], case.get("mode", "natural"))
    outcome = "approve" if result["decision"] == "approve" else "decline" if result["decision"] == "decline" else "hold"
    return case, outcome, result, time.monotonic() - started


cases = [c for c in data["cases"] for _ in range(options.repeat)]
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(run, cases))

false_approvals = [(c, r) for c, o, r, _ in results if o == "approve" and c["expect"] != "approve"]
false_rejections = [(c, o) for c, o, r, _ in results if c["expect"] == "approve" and o != "approve"]
wrong_cancels = [(c, o) for c, o, r, _ in results if o == "decline" and c["expect"] != "decline"]
other = [(c, o) for c, o, r, _ in results if c["expect"] != "approve" and o != c["expect"] and o != "approve" and o != "decline"]
latencies = sorted(t for *_, t in results)
print(f"Judge {options.judge} / verifier {options.verifier}; {len(results)} judgments, {len(calls)} model calls "
      f"({len(calls) / len(results):.2f} per judgment)")
print(f"False approvals: {len(false_approvals)}   False rejections: {len(false_rejections)} of "
      f"{sum(c['expect'] == 'approve' for c in cases)}   Quotes wrongly cancelled: {len(wrong_cancels)}   "
      f"Declines treated as hold: {len(other)}")
print(f"Latency p50 {statistics.median(latencies):.2f}s  p95 {latencies[int(len(latencies) * .95) - 1]:.2f}s  max {latencies[-1]:.2f}s")
for case, result in false_approvals:
    print("  FALSE APPROVAL:", case["kind"], case.get("mode", "natural"), repr(case["caller"]), result["judges"])
for case, outcome in false_rejections:
    print("  false rejection:", case["kind"], case.get("mode", "natural"), repr(case["caller"]), "->", outcome)
for case, outcome in wrong_cancels:
    print("  wrongly cancelled:", case["kind"], repr(case["caller"]), "expected", case["expect"])
sys.exit(1 if false_approvals else 0)
