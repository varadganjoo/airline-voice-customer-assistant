"""A bounded LangGraph execution per event, persisted atomically by Store.call.

The graph owns allowed tool transitions. Gemini still owns spoken wording.
There are no external side effects inside graph nodes: SMS/payments are fixtures.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

import consent
import telemetry
from database import Store, digest, empty_state
from tools import HANDOFF, decide, get_options, now_ms, propose
from voice import identity_readback

APPROVAL_WINDOW_MS = 60_000
# Escalation ladder for a quote whose approval was not clear. The server tracks the step per proposal.
LADDER = {
    "natural": "Nothing changed. The caller's reply was not a clear approval of this change. Briefly restate the change and "
               "its price, then ask whether they'd like you to go ahead. Do not ask for specific wording.",
    "closed": "Nothing changed. In one sentence restate the change and its exact price, refund or credit, then ask exactly: "
              "\"To make this change, please say yes. To leave your booking as it is, please say no.\" Then wait for the answer.",
    "manual": "Nothing changed. Spoken approval for this change is paused. Say exactly: \"To confirm this change, please "
              "press 1. To cancel it, press 7.\" Then wait for the key press. If they cannot use a keypad, offer a representative.",
    "notHeard": "Nothing changed. The caller has not heard the complete quote. Restate the change with its exact price, "
                "refund or credit, then ask whether to go ahead.",
    "question": "Nothing changed and the quote is still open. Answer the caller's question or condition first, then ask "
                "whether they'd like to go ahead.",
    "decline": "Nothing changed. The caller declined, so this quote is cancelled. Ask what they would like instead.",
}


def turn_fingerprint(text, proposal_id, agent_text, channel):
    """Hash of a caller turn. Transcripts and codes are never stored; the hash proves a re-sent transcript."""
    return digest(json.dumps([text, proposal_id, agent_text or "", channel or "voice"]))


class GraphState(TypedDict):
    data: dict
    event: dict
    result: dict


@dataclass(frozen=True)
class Context:
    store: Store
    account_id: str
    session_hash: str
    call_id: str
    policy: dict | None = None


def pending(data):
    return next((p for p in data["state"]["proposals"] if p["status"] == "pending" and p["expiresAt"] > now_ms()), None)


def next_stage(data):
    if data["ended"]:
        return "ended"
    if pending(data):
        return "awaiting_approval"
    if data["workflow"]["stage"] == "awaiting_code" and not data["state"].get("reservation"):
        return "awaiting_code"
    return "verified" if data["state"].get("reservation") else "identify"


def record_turn(state):
    data, event = deepcopy(state["data"]), state["event"]
    flow = data["workflow"]
    old = flow.get("turn")
    channel = event.get("channel") or "voice"
    key = event.get("keypadKey") if channel == "keypad" else None
    fingerprint = turn_fingerprint(event["text"], event.get("proposalId"), event.get("agentText"), f"keypad:{key}" if key else channel)
    # Turn IDs are one-use browser events; text is never taken from a tool argument.
    if old and old["id"] == event["turnId"]:
        if old["fingerprint"] != fingerprint:
            raise ValueError("Caller turn ID was reused with different content.")
        return {"data": data, "result": old.get("result", {"turnId": old["id"], "sequence": old["sequence"]})}
    if event["turnId"] in flow.get("seenTurns", []):
        raise ValueError("Caller turn is stale. Use the latest caller turn.")
    seq = (old["sequence"] if old else 0) + 1
    p = pending(data)
    # Bind the reply to the quote the caller was answering. Whether it approves is decided at commit time.
    target = p["id"] if p and p["id"] == event.get("proposalId") and seq > p.get("createdAfterTurn", seq) else None
    keypad_step = bool(target and p.get("approval", {}).get("mode") == "manual")
    if channel == "keypad" and (not keypad_step or key not in ("1", "7")):
        raise ValueError("The keypad is only used after the agent asks the caller to press 1 or 7.")
    flow["turn"] = {"id": event["turnId"], "sequence": seq, "proposalId": target, "sentProposalId": event.get("proposalId"),
                    "channel": channel, "keypadKey": key, "receivedAt": now_ms(), "consumed": False,
                    "identityApproved": False, "fingerprint": fingerprint}
    flow["seenTurns"] = (flow.get("seenTurns", []) + [event["turnId"]])[-128:]
    flow["lastCallerActivity"] = now_ms()
    result = {"turnId": event["turnId"], "sequence": seq}
    # At the keypad step, 7 cancels immediately (far from 1, so a slip cannot cancel). A spoken refusal also cancels, so a quote the caller turned
    # down can never be confirmed by a later key press. Spoken approval stays paused at this step.
    assessment = event.get("assessment") or {}
    spoken_no = (keypad_step and channel == "voice" and assessment.get("kind") == "change"
                 and assessment.get("proposalId") == target and assessment.get("decision") == "decline")
    if keypad_step and (key == "7" or spoken_no):
        p["status"] = "superseded"
        flow["turn"]["consumed"] = True
        result["approval"] = {"declined": True, "channel": "keypad" if key else "voice", "instruction": LADDER["decline"]}
    flow["stage"] = next_stage(data)
    candidate = flow.get("pendingIdentity")
    if candidate:
        assessment = event.get("assessment") or {}
        current = assessment.get("kind") == "identity" and assessment.get("args") == candidate["args"]
        decision = assessment["decision"] if current else "unclear"
        if now_ms() - candidate["createdAt"] > APPROVAL_WINDOW_MS or seq <= candidate["afterTurn"]:
            flow.pop("pendingIdentity", None)
            result["verificationResult"] = {"notConfirmed": True, "instruction": "No code was sent because the identity "
                "confirmation expired. Call request_verification again to prepare a fresh readback, then ask for confirmation."}
        elif decision == "approve":
            flow["turn"]["identityApproved"] = True
            result["continueVerification"] = candidate["args"]
        elif decision == "decline":
            # A correction can never approve previously collected details.
            flow.pop("pendingIdentity", None)
            result["verificationResult"] = {"notConfirmed": True, "instruction": "No code was sent. The caller said the details "
                "are not right. Collect the correction, call request_verification with the corrected details, and read them back."}
        elif decision == "notHeard":
            result["verificationResult"] = {"notConfirmed": True, "instruction": "No code was sent. Read back both the "
                "confirmation number and the full name exactly as request_verification returned them, then ask if they're correct."}
        # question/unclear: the readback stays pending; the agent answers or asks again in its own words.
    flow["turn"]["result"] = result
    return {"data": data, "result": result}


def approval_gate(data, turn, fresh_turn, proposal_id, assessment):
    """Return a result that stops the commit, or None when the caller verifiably approved this exact quote.

    Approval needs a fresh, unconsumed caller turn bound to the quote plus either a verified two-judge
    approval (spoken) or a keypad press once the ladder reaches the keypad step. Each turn gets one judgment.
    """
    p = next((p for p in data["state"]["proposals"] if p["id"] == proposal_id), None)
    if p and p["status"] == "completed":
        receipt = next((a for a in data["state"]["actions"] if a["id"] == p["id"]), None)
        return {"done": True, "receipt": receipt, "booking": data["state"]["reservation"]}
    if not p or p["status"] != "pending" or p["expiresAt"] <= now_ms():
        return {"notConfirmed": True, "requiresNewProposal": True, "instruction": "Nothing changed. This quote is unavailable, "
                "declined or expired. If the caller still wants the change, call propose_action again, describe the new quote "
                "and wait for their reply. Do not confirm the old quote again."}
    ladder = p.setdefault("approval", {"mode": "natural", "unclear": 0})
    if not (fresh_turn and turn["proposalId"] == p["id"] and turn["sequence"] > p.get("createdAfterTurn", turn["sequence"])):
        return {"notConfirmed": True, "approvalStep": ladder["mode"], "instruction": "Nothing changed. Describe the current "
                "quote and ask whether to go ahead; wait for the caller's reply before calling confirm_action."}
    if turn["channel"] == "keypad":
        # record_turn only accepts keypad turns at the keypad step; 7 has already cancelled the quote.
        decision, judges = ("approve", []) if turn.get("keypadKey") == "1" else ("unclear", [])
    elif ladder["mode"] == "manual":
        return {"notConfirmed": True, "approvalStep": "manual", "instruction": LADDER["manual"]}
    else:
        current = (assessment.get("kind") == "change" and assessment.get("proposalId") == p["id"]
                   and assessment.get("mode") == ladder["mode"])
        decision, judges = (assessment["decision"], assessment.get("judges", [])) if current else ("unclear", [])
    turn["consumed"] = True
    if decision == "approve":
        p["approvalEvidence"] = {"channel": turn["channel"], "step": ladder["mode"], "turnSequence": turn["sequence"],
                                 "judges": judges, "approvedAt": now_ms()}
        return None
    if decision == "decline":
        p["status"] = "superseded"
        return {"notConfirmed": True, "requiresNewProposal": True, "declined": True, "instruction": LADDER["decline"]}
    if decision in ("question", "notHeard"):
        return {"notConfirmed": True, "approvalStep": ladder["mode"], "instruction": LADDER[decision]}
    ladder["unclear"] += 1
    ladder["mode"] = "natural" if ladder["unclear"] < 2 else "closed" if ladder["unclear"] == 2 else "manual"
    return {"notConfirmed": True, "approvalStep": ladder["mode"], "instruction": LADDER[ladder["mode"]]}


def guard(state, runtime: Runtime[Context]):
    data, event, ctx = deepcopy(state["data"]), state["event"], runtime.context
    name, args = event["name"], event["args"]
    fingerprint = digest(json.dumps([name, args], sort_keys=True))
    old = ctx.store.tool_receipt(ctx.account_id, ctx.call_id, event["requestId"])
    if old:
        if old["fingerprint"] != "cancelled" and old["fingerprint"] != fingerprint:
            raise ValueError("Tool request ID was reused.")
        return {"data": data, "result": old["result"]}
    if event["requestId"] in data["cancelled"]:
        return {"data": data, "result": {"cancelled": True}}
    flow, turn = data["workflow"], data["workflow"].get("turn")
    sent = flow.get("identitySent")
    if name == "request_verification" and sent and sent["turnId"] == event.get("turnId") and sent["args"] == args:
        return {"data": data, "result": sent["result"]}
    resent = flow.get("resendSent")
    if name == "resend_verification" and resent and resent["turnId"] == event.get("turnId"):
        return {"data": data, "result": resent["result"]}
    if name in ("get_case", "get_options", "propose_action", "confirm_action"):
        reference = ctx.store.verified_reference(ctx.account_id, ctx.session_hash)
        if reference != (data["state"].get("reservation") or {}).get("reference"):
            raise ValueError("Verify this booking in the current call before continuing.")
        latest = ctx.store.lookup(ctx.account_id, reference)
        if latest["version"] != data["state"]["version"]:
            data["state"] = dict(latest, notes=data["state"]["notes"], interactionNotes=data["state"].get("interactionNotes", []))
    fresh_turn = bool(turn and turn["id"] == event.get("turnId") and not turn["consumed"]
                      and now_ms() - turn["receivedAt"] <= APPROVAL_WINDOW_MS)
    assessment = event.get("assessment") or {}
    if name == "resend_verification":
        if not flow.get("verificationTarget") or flow["stage"] != "awaiting_code":
            raise ValueError("Identify the booking and request its first code before asking for a resend.")
        if not (fresh_turn and assessment.get("kind") == "resend" and assessment["decision"] == "approve"):
            if fresh_turn and assessment:
                turn["consumed"] = True  # One judgment per caller turn; a repeat call cannot re-ask the classifier.
            flow["pendingResend"] = {"createdAt": now_ms()}
            return {"data": data, "result": {"notConfirmed": True, "instruction": "Nothing is wrong: no new code was sent "
                    "because none was requested, and the code already texted is still valid. Do not tell the caller anything went "
                    "wrong and do not call more tools. Wait for them to read the code or to ask for a new one."}}
    if name == "request_verification":
        candidate = flow.get("pendingIdentity")
        valid = (fresh_turn and turn.get("identityApproved") and candidate and candidate["args"] == args
                 and turn["sequence"] > candidate["afterTurn"] and now_ms() - candidate["createdAt"] <= APPROVAL_WINDOW_MS)
        if not valid:
            readback = identity_readback(args["reference"], args["name"], args.get("phonetic", "none"))
            flow["pendingIdentity"] = {"args": args, "readback": readback, "createdAt": now_ms(),
                                       "afterTurn": turn["sequence"] if turn else 0}
            return {"data": data, "result": {"notConfirmed": True, "readback": readback, "instruction": "No code sent. Speak "
                    "this readback of BOTH the name and reference once, then wait for confirmation. Read reference characters "
                    "separately. Do not request name spelling or add NATO words unless that field was unclear or corrected."}}
    if name == "confirm_action":
        rejection = approval_gate(data, turn, fresh_turn, args["proposalId"], assessment)
        if rejection:
            return {"data": data, "result": rejection}
    if name == "propose_action" and (not turn or turn["id"] != event.get("turnId")):
        raise ValueError("Wait for the latest caller turn before proposing a change.")
    if name == "verify_booking" and flow["stage"] != "awaiting_code":
        raise ValueError("Request a verification code before submitting it.")
    if name == "verify_booking" and not (turn and turn["id"] == event.get("turnId") and not turn["consumed"]):
        return {"data": data, "result": {"notConfirmed": True, "instruction": "No verification attempt was made. The caller has not read the code yet. Do not call more tools. Say one short, warm sentence such as \"No rush, I'm here whenever you have it,\" then wait for them to read all six digits; never invent a code."}}
    flow["stage"] = next_stage(data) if name != "verify_booking" else flow["stage"]
    return {"data": data}


def verify_identity(state, runtime: Runtime[Context]):
    data, event, ctx = deepcopy(state["data"]), state["event"], runtime.context
    name, args = event["name"], event["args"]
    if name in ("request_verification", "resend_verification"):
        if name == "resend_verification":
            args = data["workflow"]["verificationTarget"]
        if len(args["name"].strip().split()) < 2:
            raise ValueError("You need the passenger's first and last name and confirmation of both the name and booking reference.")
        ctx.store.limit("verification:" + ctx.session_hash, 3, 60_000)
        result = ctx.store.request_verification(ctx.account_id, ctx.session_hash, args["reference"], args["name"])
        data["state"] = dict(empty_state(), notes=data["state"]["notes"], interactionNotes=data["state"].get("interactionNotes", []))
        data["state"]["verificationPending"] = True
        data["workflow"]["verificationTarget"] = {"reference": args["reference"], "name": args["name"]}
        data["workflow"]["turn"]["consumed"] = True
        data["workflow"].pop("pendingIdentity", None)
        data["workflow"].pop("pendingResend", None)
        if name == "request_verification":
            data["workflow"]["identitySent"] = {"turnId": event.get("turnId"), "args": args, "result": result}
        else:
            data["workflow"]["resendSent"] = {"turnId": event.get("turnId"), "result": result}
        data["workflow"]["stage"] = "awaiting_code"
    else:
        result = ctx.store.verify(ctx.account_id, ctx.session_hash, args["code"])
        if result.get("verified"):
            data["state"] = dict(ctx.store.lookup(ctx.account_id, result["reference"]), notes=data["state"]["notes"], interactionNotes=data["state"].get("interactionNotes", []))
            data["workflow"]["stage"] = "verified"
            result = {"verified": True, "booking": data["state"]["reservation"]}
    return {"data": data, "result": result}


def create_proposal(state):
    data, args = deepcopy(state["data"]), state["event"]["args"]
    p = propose(data["state"], args["action"], args["segment"], args["value"], data["workflow"]["turn"]["sequence"])
    data["workflow"]["stage"] = "awaiting_approval"
    return {"data": data, "result": p}


def commit_change(state, runtime: Runtime[Context]):
    if os.getenv("BOOKING_WRITES_ENABLED", "true").lower() != "true":
        return {"result": {"error": "Booking changes are temporarily unavailable. Nothing changed.", "instruction": "Explain that changes are paused and offer a representative. Do not retry the change or claim completion."}}
    data, ctx = deepcopy(state["data"]), runtime.context
    version = data["state"]["version"]
    decide(data["state"], state["event"]["args"]["proposalId"], True)
    ctx.store.commit(ctx.account_id, data["state"]["reservation"]["reference"], version, data["state"]["reservation"], data["state"]["actions"][-1])
    data["workflow"]["turn"]["consumed"] = True
    data["workflow"]["stage"] = "verified"
    return {"data": data, "result": {"done": True, "receipt": data["state"]["actions"][-1], "booking": data["state"]["reservation"],
            "instruction": "Describe the receipt precisely. If it describes a refund request, say submitted, not settled or paid. Give only the processing estimate in the receipt. Use this distinction in notes too."}}


def read_or_handoff(state, runtime: Runtime[Context]):
    data, event = deepcopy(state["data"]), state["event"]
    name, args = event["name"], event["args"]
    if name == "lookup_policy":
        result = runtime.context.policy
    elif name == "get_case":
        result = deepcopy(data["state"])
    elif name == "get_options":
        result = get_options(data["state"]["reservation"], args["kind"], args["segment"], args["date"], args["route"])
    elif name == "save_notes":
        data["state"]["notes"] = args["notes"]
        result = {"saved": True}
    elif name == "withdraw_quote":
        # Removing a quote can never change a booking, so it needs no approval judgment.
        p = next((p for p in data["state"]["proposals"] if p["id"] == args["proposalId"] and p["status"] == "pending"), None)
        if p:
            p["status"] = "superseded"
        result = {"withdrawn": bool(p)}
    elif name == "transfer_to_representative":
        reservation = data["state"].get("reservation") or {}
        try:
            verified_reference = runtime.context.store.verified_reference(runtime.context.account_id, runtime.context.session_hash)
        except ValueError:
            verified_reference = None
        verified = bool(reservation and verified_reference == reservation.get("reference"))
        open_proposal = pending(data)
        brief = {"reason": args["reason"].strip(), "notes": args["notes"].strip(),
                 "bookingReference": verified_reference if verified else None, "bookingVerified": verified,
                 "completedActions": [action["summary"] for action in data["state"]["actions"][-10:]],
                 "unapprovedProposal": open_proposal["summary"] if open_proposal else None}
        if open_proposal:
            open_proposal["status"] = "superseded"
        data["state"]["notes"] = brief["notes"]
        data["handoff"] = brief
        data["ended"] = True
        data["workflow"]["stage"] = "ended"
        result = {"handoff": True, "message": HANDOFF, "brief": brief, "actualTransfer": False}
    else:
        raise ValueError("Invalid tool.")
    return {"data": data, "result": result}


def save_result(state, runtime: Runtime[Context]):
    data, event = deepcopy(state["data"]), state["event"]
    ctx = runtime.context
    # Every outcome is replayable; a rejected request must not later become a write.
    if not ctx.store.tool_receipt(ctx.account_id, ctx.call_id, event["requestId"]):
        ctx.store.save_tool_receipt(ctx.account_id, ctx.call_id, event["requestId"], digest(json.dumps([event["name"], event["args"]], sort_keys=True)), state["result"])
        name, args, result = event["name"], event["args"], state["result"]
        note = None
        if not result.get("cancelled") and not result.get("error"):
            if name == "save_notes":
                note = args["notes"].strip()
            elif name == "withdraw_quote" and result.get("withdrawn"):
                note = "Quote withdrawn at the customer's request; no change made."
            elif name in ("request_verification", "resend_verification") and result.get("sent"):
                note = "New verification code sent at the customer's request." if name == "resend_verification" else "Verification code sent; waiting for customer verification."
            elif name == "lookup_policy":
                note = f"Consulted {args['topic'].replace('_', ' ')} policy guidance."
            elif name == "verify_booking" and result.get("verified"):
                note = f"Verified booking {result['booking']['reference']}."
            elif name == "propose_action" and result.get("summary"):
                note = f"Proposed: {result['summary']} Awaiting customer approval; no change made."
            elif name == "confirm_action" and result.get("done") and result.get("receipt"):
                note = f"Completed: {result['receipt']['summary']}"
            elif name == "transfer_to_representative" and result.get("handoff"):
                note = f"Handoff prepared: {args['reason'].strip()}\n{args['notes'].strip()}"
        if note:
            notes = data["state"].setdefault("interactionNotes", [])
            note_id = name + "-" + event["turnId"] if name in ("request_verification", "resend_verification") else event["requestId"]
            if not any(n["id"] == note_id for n in notes):
                timestamp = max(now_ms(), notes[-1]["timestamp"] + 1 if notes else 0)
                notes.append({"id": note_id, "timestamp": timestamp,
                              "kind": "agent" if name == "save_notes" else "activity", "text": note})
                data["state"]["interactionNotes"] = notes[-200:]
    if event["name"] not in ("get_case", "lookup_policy", "verify_booking", "confirm_action") and not state["result"].get("notConfirmed"):
        data["results"] = dict(list(data["results"].items())[-31:])
        data["results"][event["requestId"]] = {"fingerprint": json.dumps([event["name"], event["args"]], sort_keys=True), "result": state["result"]}
    return {"data": data}


def route_tool(state):
    if state.get("result"):
        return "receipt"
    name = state["event"]["name"]
    return "identity" if name in ("request_verification", "resend_verification", "verify_booking") else "proposal" if name == "propose_action" else "commit" if name == "confirm_action" else "read"


def advance_verification(state, runtime: Runtime[Context]):
    """Commit approval, simulated SMS and receipt together; replay returns that receipt."""
    result = dict(state["result"])
    args = result.pop("continueVerification", None)
    if not args:
        return {}
    event = {"name": "request_verification", "args": args,
             "requestId": "identity-" + state["event"]["turnId"], "turnId": state["event"]["turnId"]}
    tool_state = {"data": state["data"], "event": event, "result": {}}
    tool_state.update(guard(tool_state, runtime))
    if not tool_state["result"]:
        tool_state.update(verify_identity(tool_state, runtime))
    tool_state.update(save_result(tool_state, runtime))
    result["verificationResult"] = tool_state["result"]
    tool_state["data"]["workflow"]["turn"]["result"] = result
    return {"data": tool_state["data"], "result": result}


builder = StateGraph(GraphState, context_schema=Context)
builder.add_node("turn", telemetry.traced("workflow.turn", record_turn))
builder.add_node("advance_verification", telemetry.traced("workflow.advance_verification", advance_verification))
builder.add_node("guard", telemetry.traced("workflow.guard", guard))
builder.add_node("identity", telemetry.traced("workflow.identity", verify_identity))
builder.add_node("proposal", telemetry.traced("workflow.proposal", create_proposal))
builder.add_node("commit", telemetry.traced("workflow.commit", commit_change))
builder.add_node("read", telemetry.traced("workflow.read", read_or_handoff))
builder.add_node("receipt", telemetry.traced("workflow.receipt", save_result))
builder.add_conditional_edges(START, lambda s: "turn" if s["event"].get("kind") == "turn" else "guard", {"turn": "turn", "guard": "guard"})
builder.add_edge("turn", "advance_verification")
builder.add_edge("advance_verification", END)
builder.add_conditional_edges("guard", route_tool, {"receipt": "receipt", "identity": "identity", "proposal": "proposal", "commit": "commit", "read": "read"})
for node in ("identity", "proposal", "commit", "read"):
    builder.add_edge(node, "receipt")
builder.add_edge("receipt", END)
GRAPH = builder.compile()


def assess_event(store, account_id, call_id, event, assess):
    """Run approval judgments outside the write transaction, only when something is being approved.

    The graph re-checks inside the transaction that the judged subject is still current, so a stale
    judgment can never approve a different quote, readback or step.
    """
    rows = store.rows("SELECT data FROM calls WHERE id=? AND account_id=? AND expires>?", call_id, account_id, now_ms())
    flow = json.loads(rows[0]["data"]).get("workflow") if rows else None
    if not flow:
        return event
    data = json.loads(rows[0]["data"])
    turn = flow.get("turn")
    if event.get("kind") == "turn":
        voice = (event.get("channel") or "voice") == "voice"
        p = next((p for p in data["state"]["proposals"] if p["id"] == event.get("proposalId") and p["status"] == "pending"
                  and p["expiresAt"] > now_ms()), None)
        if p and voice and p.get("approval", {}).get("mode") == "manual" and not (turn and turn["id"] == event["turnId"]):
            # Only a refusal is acted on at the keypad step; judged as a closed yes/no reply.
            judgment = assess("change", p["summary"], event.get("agentText"), event["text"], "closed")
            return dict(event, assessment=dict(judgment, kind="change", proposalId=p["id"], mode="manual"))
        candidate = flow.get("pendingIdentity")
        replay = turn and turn["id"] == event["turnId"]
        if not candidate or replay or (event.get("channel") or "voice") != "voice" or not candidate.get("readback"):
            return event
        judgment = assess("identity", candidate["readback"], event.get("agentText"), event["text"])
        return dict(event, assessment=dict(judgment, kind="identity", args=candidate["args"]))
    name = event.get("name")
    if name not in ("confirm_action", "resend_verification") or not turn or turn["id"] != event.get("turnId"):
        return event
    if turn["consumed"] or turn["channel"] != "voice":
        return event
    # The browser re-sends the caller's words; only text matching the saved turn hash is ever judged.
    text, agent_text = event.get("turnText") or "", event.get("agentText") or ""
    if turn["fingerprint"] != turn_fingerprint(text, turn.get("sentProposalId"), agent_text, "voice") or not text.strip():
        return event
    if name == "resend_verification":
        offer = flow.get("pendingResend")
        offered = bool(offer and now_ms() - offer["createdAt"] <= APPROVAL_WINDOW_MS)
        subject = "The agent offered to text a new code." if offered else "No new code has been offered yet."
        return dict(event, assessment=dict(assess("resend", subject, agent_text, text), kind="resend"))
    p = next((p for p in data["state"]["proposals"] if p["id"] == event["args"].get("proposalId")), None)
    if not p or p["status"] != "pending" or turn["proposalId"] != p["id"]:
        return event
    mode = p.get("approval", {}).get("mode", "natural")
    if mode == "manual":
        return event
    judgment = assess("change", p["summary"], agent_text, text, mode)
    return dict(event, assessment=dict(judgment, kind="change", proposalId=p["id"], mode=mode))


def process_event(store, account_id, session_hash, call_id, event, policy=None, assess=None):
    # Judgments are produced here, never accepted from a request body.
    with telemetry.span("workflow.assess"):
        event = assess_event(store, account_id, call_id, {k: v for k, v in event.items() if k != "assessment"},
                             assess or consent.assess)
        if event.get("assessment"):
            telemetry.annotate(kind=event["assessment"].get("kind"), decision=event["assessment"].get("decision"), mode=event["assessment"].get("mode"))
    with telemetry.span("workflow.graph", event=event.get("kind") or event.get("name")), store.call(account_id, call_id) as persisted:
        output = GRAPH.invoke({"data": persisted, "event": event, "result": {}}, config={"recursion_limit": 16}, context=Context(store, account_id, session_hash, call_id, policy))
        persisted.clear()
        persisted.update(output["data"])
        return {"result": output["result"], "state": persisted["state"], "workflow": {"stage": persisted["workflow"]["stage"]}}
