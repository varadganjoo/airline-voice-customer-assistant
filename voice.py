"""Live audio configuration. Long-lived credentials are used only by the server."""

from pathlib import Path
import re

from tools import ACTIONS, POLICIES

INSTRUCTIONS = Path(__file__).with_name("instructions.txt").read_text(encoding="utf-8")


def string(description, enum=None):
    return {"type": "STRING", "description": description, **({"enum": enum} if enum else {})}


def declaration(name, description, properties, optional=(), behavior="NON_BLOCKING"):
    return {"name": name, "description": description,
            "behavior": behavior,
            "parameters": {"type": "OBJECT", "properties": properties, "required": [key for key in properties if key not in optional]}}


DECLARATIONS = [
    declaration("lookup_policy", "Read the reviewed airline policy summary before explaining rules, fees or eligibility. Give one short spoken acknowledgment in the caller's current conversational language as you start this non-blocking lookup; use that same language for the answer. Do not silently search or repeat an acknowledgment already given.", {"topic": string("Policy topic", list(POLICIES))}),
    declaration("request_verification", "Prepare confirmation of BOTH the six-character reference and full name. Normally accept the spoken name without asking for spelling. Use phonetic readback only for a field you struggled to hear or whose readback the caller corrected. Speak the returned readback and wait for a fresh yes; only sent=true means a code was sent.", {"reference": string("Six-character confirmation number collected from the caller"), "name": string("Passenger first and last name collected from the caller"), "phonetic": string("Omit or use none normally. Select only the field needing clarification after unclear hearing or a correction.", ["none", "name", "reference", "both"])}, optional=("phonetic",)),
    declaration("verify_booking", "Submit the six-digit code the caller reads. Returns the booking when correct.", {"code": string("Six digits exactly as the caller said them")}),
    declaration("resend_verification", "Send a NEW code for the booking already identified in this call, only when the caller requests a resend or agrees to your resend offer. Do not repeat request_verification. Wait for sent=true before saying it was sent.", {}),
    declaration("get_case", "Current verified booking, pending proposals and completed actions.", {}),
    declaration("get_options", 'Look up availability before a change. Flights/seats need a segment; date is YYYY-MM-DD; route is like DEN-ORD. Use empty strings for unused fields.', {"kind": string("Options to look up", ["flights", "seats", "upgrades", "meals", "bags", "assistance", "pets"]), "segment": string('"1", "2", "all", or ""'), "date": string('YYYY-MM-DD or ""'), "route": string('New route like DEN-ORD, or ""')}),
    declaration("propose_action", 'Price a change without making it. Values: change_flight=option ID; change_seat=seat; upgrade=premium_economy|first|business; special_meal=meal code or NONE; add_bags=total count; special_assistance=code or remove:CODE; pet=dog|cat|none; loyalty=number or NONE; cancel_for_credit/cancel_and_refund="". Use cancel_and_refund for money-back requests.', {"action": string("Action", ACTIONS), "segment": string('"1", "2", "all", or ""'), "value": string("Action value")}),
    declaration("confirm_action", "Ask to commit the quoted change after the caller answers your quote with agreement in any words. The application independently verifies the approval and may instead return an instruction to restate the quote, ask yes or no, or use the keypad. An earlier yes cannot approve a newer quote.", {"proposalId": string("ID returned by propose_action")}),
    declaration("withdraw_quote", "Silently withdraw a quote the caller declined or no longer wants. A question or hesitation about the price is not a decline: answer it and ask again instead. Nothing on the booking changes. Do not announce this call.", {"proposalId": string("ID returned by propose_action")}),
    declaration("save_notes", "Silently append a timestamped interaction note on every call: caller goal, new context, resolution or next steps. Do not repeat prior notes. Never store codes or payment details.", {"notes": string("New customer interaction facts and pending questions; max 2000 characters")}),
    declaration("transfer_to_representative", "Immediately initiate the handoff when the caller requests a human or you decide to transfer. This is BLOCKING: invoke it now with the case brief, then wait. Do not merely promise a transfer or ask for another turn. On success, speak the returned transfer message once in the caller's language. The application waits for your speech to finish before ending the AI call.", {"reason": string("Short reason for handoff"), "notes": string("Caller goal, relevant facts, completed steps and outcomes, unresolved issue, and suggested next step; no verification codes or payment details")}, behavior="BLOCKING"),
]


def validate_tool(name, args):
    tool = next((t for t in DECLARATIONS if t["name"] == name), None)
    if not tool or not isinstance(args, dict):
        raise ValueError("Invalid tool or arguments.")
    props = tool["parameters"]["properties"]
    if not set(tool["parameters"]["required"]) <= args.keys() or not args.keys() <= props.keys():
        raise ValueError("Missing or unexpected arguments.")
    for key in args:
        schema = props[key]
        if not isinstance(args[key], str) or len(args[key]) > (2000 if key == "notes" else 300):
            raise ValueError(f"Invalid {key}.")
        if "enum" in schema and args[key] not in schema["enum"]:
            raise ValueError(f"Invalid {key}.")
    if name == "transfer_to_representative" and (not args["reason"].strip() or not args["notes"].strip()):
        raise ValueError("A handoff reason and case notes are required before transfer.")
    if name == "save_notes" and not args["notes"].strip():
        raise ValueError("An interaction note cannot be empty.")
    if name == "request_verification" and not re.fullmatch(r"[A-Za-z0-9]{6}", args["reference"]):
        raise ValueError("The confirmation number must contain exactly six letters or digits. Ask the caller to spell it again; do not guess or add characters.")
    if name in ("save_notes", "transfer_to_representative") and re.search(r"\b\d{6}\b", args["notes"]):
        raise ValueError("Remove verification codes from case notes before saving.")


def live_config(handle=None):
    return {"responseModalities": ["AUDIO"], "systemInstruction": INSTRUCTIONS,
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": False,
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW", "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                "prefixPaddingMs": 300, "silenceDurationMs": 1500}, "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"},
            "contextWindowCompression": {"triggerTokens": "24000", "slidingWindow": {"targetTokens": "12000"}},
            "sessionResumption": {"handle": handle} if handle else {}, "tools": [{"functionDeclarations": DECLARATIONS}]}


_NATO = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu".split()))
_DIGITS = dict(zip("0123456789", "zero one two three four five six seven eight nine".split()))


def identity_readback(reference, name, phonetic="none"):
    def spell(value, nato):
        return ", ".join(_DIGITS.get(c, _NATO.get(c, c) if nato else c) for c in value.upper() if c.isalnum())
    names = "spelled " + "; ".join(spell(word, True) for word in name.split()) if phonetic in ("name", "both") else name
    return f"The confirmation number is {spell(reference, phonetic in ('reference', 'both'))}. The passenger name is {names}. Is that correct?"
