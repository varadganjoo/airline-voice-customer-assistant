"""Demo inventory and booking changes. All prices are simulated integer dollars."""

from copy import deepcopy
from datetime import date as Date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import secrets
import time
from uuid import uuid4


HANDOFF = "The call is being forwarded to a customer service representative now."
_policy_data = json.loads(Path(__file__).with_name("policies.json").read_text(encoding="utf-8"))
POLICIES, REVIEWED = _policy_data["policies"], _policy_data["reviewed"]

def now_ms():
    return int(time.time() * 1000)


def lookup_policy(topic):
    if topic not in POLICIES:
        raise ValueError("Unknown policy topic.")
    source = POLICIES[topic]
    reviewed = source.get("reviewed", REVIEWED)
    result = {"title": source["title"], "status": "snapshot", "reviewedAt": reviewed,
              "facts": deepcopy(source["facts"]),
              "instruction": "Reviewed airline policy summary. Explain only these facts; offer a representative for anything beyond them."}
    if (Date.today() - Date.fromisoformat(reviewed)).days > 30:
        result.update(status="unavailable", facts=[], instruction="The reviewed policy summary is too old. Do not guess fees or eligibility; offer a representative.")
    return result


# shortcut: fixed summer UTC offsets for simulated schedules; use zoneinfo for real inventory.
AIRPORTS = {
    "DEN": ("Denver", 39.86, -104.67, -6), "SFO": ("San Francisco", 37.62, -122.38, -7),
    "ORD": ("Chicago O'Hare", 41.98, -87.9, -5), "EWR": ("Newark", 40.69, -74.17, -4),
    "IAH": ("Houston", 29.99, -95.34, -5), "IAD": ("Washington Dulles", 38.95, -77.46, -4),
    "LAX": ("Los Angeles", 33.94, -118.41, -7), "SEA": ("Seattle", 47.45, -122.31, -7),
    "BOS": ("Boston", 42.36, -71.01, -4), "MCO": ("Orlando", 28.43, -81.31, -4),
    "LAS": ("Las Vegas", 36.08, -115.15, -7), "HNL": ("Honolulu", 21.32, -157.92, -10),
    "CUN": ("Cancún", 21.04, -86.87, -5), "MEX": ("Mexico City", 19.44, -99.07, -6),
    "YVR": ("Vancouver", 49.19, -123.18, -7), "LHR": ("London Heathrow", 51.47, -0.45, 1),
    "FRA": ("Frankfurt", 50.04, 8.56, 2), "CDG": ("Paris Charles de Gaulle", 49.01, 2.55, 2),
    "NRT": ("Tokyo Narita", 35.77, 140.39, 9), "SYD": ("Sydney", -33.94, 151.18, 10),
}
ROUTES = [("DEN", "SFO"), ("ORD", "LAX"), ("EWR", "LHR"), ("IAH", "CUN"), ("SFO", "NRT"),
          ("IAD", "FRA"), ("EWR", "MCO"), ("LAX", "HNL"), ("ORD", "BOS"), ("SEA", "EWR"),
          ("DEN", "LAS"), ("IAH", "MEX"), ("SFO", "SYD"), ("EWR", "CDG"), ("ORD", "YVR")]
PET_BANNED = {"HNL", "LHR", "SYD"}
CABINS = {"economy": "Economy", "premium_economy": "Premium Economy", "first": "First Class", "business": "Business"}
CABIN_RANK = {"economy": 0, "premium_economy": 1, "first": 2, "business": 2}
MEALS = {"VGML": "Vegan", "VLML": "Vegetarian (lacto-ovo)", "AVML": "Asian vegetarian (Hindu style)", "HNML": "Hindu non-vegetarian", "KSML": "Kosher", "MOML": "Muslim (halal)", "GFML": "Gluten-free", "DBML": "Diabetic", "LSML": "Low sodium", "CHML": "Child meal", "BBML": "Baby meal", "NONE": "Standard meal (remove special meal)"}
ASSISTANCE = {"WCHR": "Wheelchair to and from the aircraft door (can climb stairs and walk to seat)", "WCHS": "Wheelchair; cannot climb stairs, can walk to seat", "WCHC": "Wheelchair; needs help all the way to the seat", "WCMP": "Travelling with own manual wheelchair", "WCBW": "Travelling with own battery-powered wheelchair", "BLND": "Blind or low vision assistance", "DEAF": "Deaf or hard of hearing assistance", "DPNA": "Intellectual or developmental disability assistance", "SVAN": "Travelling with a trained service dog", "MAAS": "Meet and assist at the airport"}
ACTIONS = ["change_flight", "change_seat", "upgrade", "special_meal", "add_bags", "special_assistance", "pet", "loyalty", "cancel_for_credit", "cancel_and_refund"]


def hashed(*parts):
    return int.from_bytes(sha256("|".join(map(str, parts)).encode()).digest()[:4], "big")


def round5(n):
    return math.floor(n / 5 + .5) * 5


def distance_km(a, b):
    _, lat1, lon1, _ = AIRPORTS[a]
    _, lat2, lon2, _ = AIRPORTS[b]
    r = math.pi / 180
    h = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def long_haul(a, b):
    return distance_km(a, b) > 4500


def premium_cabin(a, b):
    return "business" if long_haul(a, b) else "first"


def local_time(date, minutes):
    d = datetime.fromisoformat(date) + timedelta(minutes=minutes)
    return f"{d:%A}, {d:%B} {d.day}, {d.year} at {d.hour % 12 or 12}:{d:%M} {d:%p}"


def flight_options(a, b, date, waived=False, cabin="economy"):
    if a not in AIRPORTS or b not in AIRPORTS or a == b:
        raise ValueError("No Skyway schedule is available for that route. Served airports: " + ", ".join(AIRPORTS))
    try:
        day = Date.fromisoformat(date)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError()
    except ValueError:
        raise ValueError("Use a valid date in YYYY-MM-DD format.") from None
    if not 0 <= (day - datetime.now(timezone.utc).date()).days <= 330:
        raise ValueError("Flights can only be booked from today up to 330 days ahead.")
    km = distance_km(a, b)
    duration = math.floor(40 + km / 14 + .5)
    shift = (AIRPORTS[b][3] - AIRPORTS[a][3]) * 60
    times = [v for i, v in enumerate([375, 480, 610, 745, 900, 1050, 1235]) if hashed(a, b, date, i) % 3 or i == 2]
    result = []
    for i, minutes in enumerate(times):
        depart = minutes + hashed(a, b, date, "t", i) % 12 * 5
        number = 200 + hashed(a, b, date, "n", i) % 2400
        result.append({"id": f"{date}-{a}{b}-{number}", "flight": f"SW {number}", "from": a, "to": b, "date": date,
                       "departs": local_time(date, depart), "arrives": local_time(date, depart + duration + shift),
                       "durationMinutes": duration, "aircraft": "Boeing 787-9" if km > 4500 else "Boeing 737 MAX 9" if km > 2500 else "Airbus A320",
                       "fareDifferenceUSD": 0 if waived else round5(hashed(a, b, date, "p", i) % 260 - 60) * (1 if cabin == "economy" else 2)})
    return result


def seat_sections(seg):
    a, b = seg["from"], seg["to"]
    km = distance_km(a, b)
    return [
        {"name": CABINS[premium_cabin(a, b)], "cabin": premium_cabin(a, b), "rows": (1, 6), "letters": "ACDF", "priceUSD": 0},
        *([{"name": CABINS["premium_economy"], "cabin": "premium_economy", "rows": (8, 11), "letters": "ABCDEF", "priceUSD": 0}] if long_haul(a, b) else []),
        {"name": "Extra Legroom (extra legroom)", "cabin": "economy", "rows": (12, 19), "letters": "ABCDEF", "priceUSD": round5(35 + km / 60)},
        {"name": "Preferred Economy (toward the front)", "cabin": "economy", "rows": (20, 24), "letters": "ABCDEF", "priceUSD": round5(20 + km / 250)},
        {"name": "Standard Economy", "cabin": "economy", "rows": (25, 40), "letters": "ABCDEF", "priceUSD": 0},
    ]


def seat_type(letter, letters):
    return "window" if letter in (letters[0], letters[-1]) else "aisle" if letter in "CD" else "middle"


def find_seat(seg, seat):
    match = re.fullmatch(r"(\d{1,2})([A-F])", re.sub(r"\s+", "", seat.upper()))
    if match:
        row, letter = int(match[1]), match[2]
        for section in seat_sections(seg):
            if section["rows"][0] <= row <= section["rows"][1] and letter in section["letters"]:
                return {"seat": f"{row}{letter}", "section": section, "type": seat_type(letter, section["letters"])}
    return None


def available_seats(seg, section):
    result = []
    for row in range(section["rows"][0], section["rows"][1] + 1):
        for letter in section["letters"]:
            seat = f"{row}{letter}"
            if seat != seg["seat"] and hashed(seg["flight"], seg["date"], seat) % 100 >= 62:
                result.append(f"{seat} {seat_type(letter, section['letters'])}")
    return result


def assign_seat(seg, cabin, prefer=None):
    sections = [s for s in seat_sections(seg) if s["cabin"] == cabin]
    if cabin == "economy":
        sections.reverse()
    for section in sections:
        seats = [s.split() for s in available_seats(seg, section)]
        if seats:
            return next((s[0] for s in seats if s[1] == prefer), seats[0][0])
    return "assigned at the gate"


def free_bags(seg):
    return 2 if seg["cabinCode"] != "economy" else 1 if long_haul(seg["from"], seg["to"]) else 0


def bag_fee(seg, count):
    return sum([45, 55, 150, 150, 150][:max(0, count - free_bags(seg))])


def upgrade_price(seg, cabin):
    km = distance_km(seg["from"], seg["to"])
    return round5(90 + km * .07 if cabin == "premium_economy" else 350 + km * .25 if cabin == "business" else 70 + km * .12)


def generate_booking(passenger, route=None, reference=None, disrupted=False, refundable=True):
    a, b = route or secrets.choice(ROUTES)
    day = datetime.now(timezone.utc).date() + timedelta(days=4 + secrets.randbelow(56))
    cabin = "economy" if secrets.randbelow(4) else secrets.choice(["premium_economy", "business"]) if long_haul(a, b) else "first"
    segments = []
    journeys = [(a, b, day)]
    if secrets.randbelow(4):
        journeys.append((b, a, day + timedelta(days=3 + secrets.randbelow(9))))
    for origin, dest, date in journeys:
        option = secrets.choice(flight_options(origin, dest, str(date)))
        seg = {k: option[k] for k in ("flight", "from", "to", "date", "departs", "arrives", "durationMinutes", "aircraft")}
        seg.update(fromCity=AIRPORTS[origin][0], toCity=AIRPORTS[dest][0], cabin=CABINS[cabin], cabinCode=cabin,
                   seat="", seatFeeUSD=0, meal="", flightStatus="On time")
        seg["seat"] = assign_seat(seg, cabin, secrets.choice(["window", "aisle"]))
        segments.append(seg)
    if disrupted:
        segments[0]["flightStatus"] = secrets.choice(["Cancelled by Skyway (crew availability)", "Delayed 6 hours 20 minutes (aircraft maintenance)"])
    fare = sum(round5((90 + distance_km(s["from"], s["to"]) * .11) * {"economy": 1, "premium_economy": 1.8, "first": 2.6, "business": 4}[cabin]) for s in segments)
    return {"reference": reference or "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)), "passenger": passenger,
            "status": "confirmed", "fare": f"{CABINS[cabin]} ({'fully refundable' if refundable else 'nonrefundable'} fare)",
            "fareRules": "Refundable before departure." if refundable else "Nonrefundable. Voluntary cancellation before departure permits flight credit; cash refund requires a qualifying exception.",
            "cancellation": {"source": "demo_inventory", "issuer": "Skyway", "purchasedDirect": True, "unused": True,
                "refundable": refundable, "creditAllowed": True, "creditFeeUSD": 0,
                "creditExpiresOn": str(datetime.now(timezone.utc).date() + timedelta(days=325)),
                "purchasedAt": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
                "originalDepartureAt": datetime.combine(day, datetime.min.time(), timezone.utc).isoformat(),
                "qualifyingDisruption": disrupted, "alternativeAccepted": False},
            "paidUSD": fare, "creditUSD": 0, "refundRequestedUSD": 0,
            "bookedOn": str(datetime.now(timezone.utc).date() - timedelta(days=40)),
            "payment": f"Visa ending in {1000 + secrets.randbelow(9000)}", "mobile": f"ending in {1000 + secrets.randbelow(9000)}",
            "checkedBags": 0, "pet": "none", "loyaltyNumber": "", "assistance": [], "segments": segments}


def trip_label(r):
    s = r["segments"][0]
    suffix = " · " + r["status"].replace("_", " ") if r["status"] != "confirmed" else " · disrupted" if any(s["flightStatus"] != "On time" for s in r["segments"]) else ""
    fare = " · refundable" if r.get("cancellation", {}).get("refundable") else " · nonrefundable" if "cancellation" in r else ""
    return f"{s['from']} → {s['to']}{' round trip' if len(r['segments']) > 1 else ' one way'} · {s['date']}{fare}{suffix}"


def pick_segments(res, segment, single=False):
    if not single and segment in ("", "all"):
        return list(range(len(res["segments"])))
    if not re.fullmatch(r"[1-9]\d*", segment) or int(segment) > len(res["segments"]):
        raise ValueError("Specify which flight: " + "; ".join(f"{i+1} = {s['flight']} {s['from']}-{s['to']} on {s['date']}" for i, s in enumerate(res["segments"])))
    return [int(segment) - 1]


def get_options(res, kind, segment, date, route):
    idx = pick_segments(res, segment, kind in ("flights", "seats"))
    seg = res["segments"][idx[0]]
    if kind == "flights":
        codes = re.findall(r"\b[A-Z]{3}\b", route.upper())
        a, b = codes if len(codes) == 2 else (seg["from"], codes[0]) if len(codes) == 1 else (seg["from"], seg["to"])
        return {"note": "Offer two or three matching options; never read IDs aloud.", "current": f"{seg['flight']} {seg['departs']}",
                "options": [o for o in flight_options(a, b, date or seg["date"], seg["flightStatus"] != "On time", seg["cabinCode"]) if o["flight"] != seg["flight"] or o["date"] != seg["date"]]}
    if kind == "seats":
        return {"flight": seg["flight"], "cabin": seg["cabin"], "currentSeat": seg["seat"], "sections": [
            {"name": s["name"], "priceUSD": s["priceUSD"], **({"available": available_seats(seg, s)[:12]} if s["cabin"] == seg["cabinCode"] else {"note": "Requires a cabin upgrade first."})} for s in seat_sections(seg)]}
    if kind == "upgrades":
        return {"segments": [{"segment": i + 1, "flight": s["flight"], "currentCabin": s["cabin"], "options": [
            {"value": c, "cabin": CABINS[c], "priceUSD": upgrade_price(s, c) - s["seatFeeUSD"]} for c in CABINS
            if CABIN_RANK[c] > CABIN_RANK[s["cabinCode"]] and (long_haul(s["from"], s["to"]) if c == "premium_economy" else c == premium_cabin(s["from"], s["to"]))]} for i in idx for s in [res["segments"][i]]]}
    if kind == "meals":
        return {"note": "Special meals should be requested at least 24 hours before departure.", "current": [{"segment": i + 1, "meal": res["segments"][i]["meal"] or "standard"} for i in idx], "options": MEALS}
    if kind == "bags":
        return {"current": res["checkedBags"], "freeBagsPerFlight": [f"{s['flight']}: {free_bags(s)}" for s in res["segments"]], "feesPerFlightUSD": "beyond the free allowance: first paid bag $45, second $55, each additional $150", "note": "Fee applies per flight (each direction)."}
    if kind == "assistance":
        return {"current": res["assistance"], "options": ASSISTANCE}
    if kind == "pets":
        return {"current": res["pet"], "feeUSD": 150, "perFlight": True, "note": "Cats and dogs only, in cabin, in an approved carrier under the seat.", "blocked": [f"{s['flight']} {s['from']}-{s['to']}" for s in res["segments"] if s["from"] in PET_BANNED or s["to"] in PET_BANNED]}
    raise ValueError("Unknown option type.")


def money(n):
    return f"${n} will be charged to the card on file" if n > 0 else f"${-n} will be refunded to the card on file" if n < 0 else "no charge"


def cancellation_basis(res, action):
    """Use inventory evidence, never the model's reason or a policy-page instruction."""
    review = "A representative must review this ticket before cancellation or refund. Nothing has changed."
    facts = res.get("cancellation", {})
    if facts.get("issuer") != "Skyway" or facts.get("purchasedDirect") is not True or facts.get("unused") is not True:
        raise ValueError("Ticket issuer or unused-ticket eligibility needs review. " + review)
    if res["status"] != "confirmed" or res.get("creditUSD", 0):
        raise ValueError("Existing credits or previously cancelled tickets require separate refund valuation. " + review)
    if type(facts.get("alternativeAccepted")) is not bool:
        raise ValueError(review)
    current = datetime.now(timezone.utc)
    try:
        purchased = datetime.fromisoformat(facts["purchasedAt"])
        departure = datetime.fromisoformat(facts["originalDepartureAt"])
        if purchased.tzinfo is None or departure.tzinfo is None or purchased > current:
            raise ValueError()
        # Date-only legacy segments cannot establish a same-day departure cutoff.
        before_departure = all(Date.fromisoformat(s["date"]) > current.date() for s in res["segments"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Ticket timing is incomplete. " + review) from None
    if action == "cancel_for_credit":
        if not before_departure or facts.get("creditAllowed") is not True:
            raise ValueError("This ticket is not automatically eligible for voluntary travel credit. " + review)
        fee = facts.get("creditFeeUSD")
        try:
            valid_credit = Date.fromisoformat(facts["creditExpiresOn"]) > current.date()
        except (KeyError, TypeError, ValueError):
            valid_credit = False
        if type(fee) is not int or not 0 <= fee < res["paidUSD"] or not valid_credit:
            raise ValueError("Credit terms or fees need review. " + review)
        return "ticket credit terms"
    if facts.get("qualifyingDisruption") is True and facts["alternativeAccepted"] is False:
        return "verified airline cancellation or significant disruption; caller declines travel"
    if before_departure and facts.get("refundable") is True:
        return "refundable fare before departure"
    if before_departure and timedelta(0) <= current - purchased <= timedelta(hours=24) and departure - purchased >= timedelta(days=7):
        return "24-hour cancellation of a direct Skyway purchase at least seven days before departure"
    raise ValueError("This ticket does not qualify for an automatic cash-refund proposal. Check ticket credit terms. Death, certain illnesses, jury duty or changed military orders may warrant a documented exception reviewed by a representative; do not approve one from the caller's statement alone.")


def plan(res, action, segment, value):
    if res["status"] == "refund_requested":
        raise ValueError("A refund has already been requested for this booking; nothing else can be changed.")
    if res["status"] == "cancelled" and action != "cancel_and_refund":
        raise ValueError("This booking is cancelled. Only a refund request is possible.")
    updated = deepcopy(res)
    v, amount = value.strip(), 0
    idx = [] if action in ("cancel_for_credit", "cancel_and_refund", "add_bags", "pet", "loyalty") else pick_segments(res, segment, action in ("change_flight", "change_seat"))
    def label(i):
        s = res["segments"][i]
        return f"{s['flight']} {s['from']} to {s['to']}"
    if action == "change_flight":
        i = idx[0]
        seg = res["segments"][i]
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-([A-Z]{3})([A-Z]{3})-(\d+)", v)
        option = next((o for o in flight_options(m[2], m[3], m[1], seg["flightStatus"] != "On time", seg["cabinCode"]) if o["id"] == v), None) if m else None
        if not option:
            raise ValueError('Unknown flight option. Call get_options with kind "flights" and use one of its IDs.')
        if option["flight"] == seg["flight"] and option["date"] == seg["date"]:
            raise ValueError("The caller is already booked on that flight.")
        seat = find_seat(seg, seg["seat"])
        target = updated["segments"][i]
        target.update({k: option[k] for k in ("flight", "from", "to", "date", "departs", "arrives", "durationMinutes", "aircraft")})
        target.update(fromCity=AIRPORTS[option["from"]][0], toCity=AIRPORTS[option["to"]][0], flightStatus="On time", seatFeeUSD=0)
        target["seat"] = assign_seat(target, seg["cabinCode"], seat["type"] if seat else None)
        amount = option["fareDifferenceUSD"]
        pricing = f"Fare is ${-amount} lower; the difference becomes a future flight credit" if amount < 0 else f"Fare difference: ${amount} charged to the card on file" if amount else "No fare difference"
        summary = f"Change {label(i)} ({seg['departs']}) to {option['flight']} {option['from']} to {option['to']}, departing {option['departs']}, arriving {option['arrives']}. New seat {target['seat']}. {pricing}. No change fee."
        if amount < 0:
            updated["creditUSD"] += -amount
    elif action == "change_seat":
        i = idx[0]
        seg, found = res["segments"][i], find_seat(res["segments"][i], v)
        if not found:
            raise ValueError("That seat does not exist on this aircraft.")
        if found["section"]["cabin"] != seg["cabinCode"]:
            raise ValueError(f"Seat {found['seat']} is in {found['section']['name']}; offer an upgrade first.")
        if found["seat"] == seg["seat"]:
            raise ValueError(f"The caller is already in {found['seat']}.")
        if hashed(seg["flight"], seg["date"], found["seat"]) % 100 < 62:
            raise ValueError(f"Seat {found['seat']} is taken. Offer seats from get_options.")
        amount = found["section"]["priceUSD"] - seg["seatFeeUSD"]
        updated["segments"][i].update(seat=found["seat"], seatFeeUSD=found["section"]["priceUSD"])
        summary = f"Move from seat {seg['seat']} to {found['seat']} ({found['type']}, {found['section']['name']}) on {label(i)}: {money(amount)}."
    elif action == "upgrade":
        cabin = re.sub(r"[\s-]+", "_", v.lower())
        if cabin not in CABINS:
            raise ValueError("Upgrade value must be premium_economy, first or business.")
        parts = []
        for i in idx:
            seg = res["segments"][i]
            if CABIN_RANK[cabin] <= CABIN_RANK[seg["cabinCode"]]:
                raise ValueError(f"{label(i)} is already in {seg['cabin']}.")
            if not (long_haul(seg["from"], seg["to"]) if cabin == "premium_economy" else cabin == premium_cabin(seg["from"], seg["to"])):
                raise ValueError(f"{CABINS[cabin]} is not offered on {label(i)}. Use get_options.")
            amount += upgrade_price(seg, cabin) - seg["seatFeeUSD"]
            target, old_seat = updated["segments"][i], find_seat(seg, seg["seat"])
            target.update(cabin=CABINS[cabin], cabinCode=cabin, seatFeeUSD=0)
            target["seat"] = assign_seat(target, cabin, old_seat["type"] if old_seat else None)
            parts.append(f"{label(i)} to {CABINS[cabin]}, seat {target['seat']}")
        summary = f"Upgrade {'; '.join(parts)}: {money(amount)}."
    elif action == "special_meal":
        code = v.upper()
        if code not in MEALS:
            raise ValueError("Meal must be one of " + ", ".join(MEALS))
        for i in idx:
            updated["segments"][i]["meal"] = "" if code == "NONE" else f"{MEALS[code]} ({code})"
        summary = f"{'Remove the special meal from' if code == 'NONE' else 'Request a ' + MEALS[code] + ' meal on'} {' and '.join(label(i) for i in idx)}: no charge."
    elif action == "add_bags":
        if not re.fullmatch(r"[0-5]", v):
            raise ValueError("Value must be the total number of checked bags, 0 to 5.")
        count = int(v)
        if count == res["checkedBags"]:
            raise ValueError(f"The booking already has {count} checked bag(s).")
        amount = sum(bag_fee(s, count) - bag_fee(s, res["checkedBags"]) for s in res["segments"])
        updated["checkedBags"] = count
        summary = f"Change prepaid checked bags from {res['checkedBags']} to {count} for the whole trip: {money(amount)}."
    elif action == "special_assistance":
        m = re.fullmatch(r"(remove:)?\s*([A-Za-z]{4})", v)
        if not m or m[2].upper() not in ASSISTANCE:
            raise ValueError("Value must be one of " + ", ".join(ASSISTANCE) + ", or remove:CODE.")
        remove, code = bool(m[1]), m[2].upper()
        if (code in res["assistance"]) != remove:
            raise ValueError("That service is not on the booking." if remove else "That service is already on the booking.")
        updated["assistance"] = [a for a in res["assistance"] if a != code] if remove else res["assistance"] + [code]
        summary = f"{'Remove' if remove else 'Add'} {ASSISTANCE[code]} ({code}) {'from' if remove else 'to'} every flight: no charge."
    elif action == "pet":
        pet = v.lower()
        if pet not in ("dog", "cat", "none"):
            raise ValueError("Only a cat or dog can travel in cabin; value must be dog, cat or none.")
        if pet == res["pet"]:
            raise ValueError("No change.")
        banned = next((s for s in res["segments"] if s["from"] in PET_BANNED or s["to"] in PET_BANNED), None)
        if pet != "none" and banned:
            airport = banned["to"] if banned["to"] in PET_BANNED else banned["from"]
            raise ValueError(f"Skyway does not allow pets to or from {AIRPORTS[airport][0]}.")
        amount = (-150 if pet == "none" else 150 if res["pet"] == "none" else 0) * len(res["segments"])
        updated["pet"] = pet
        summary = f"{'Remove' if pet == 'none' else 'Add'} an in-cabin {res['pet'] if pet == 'none' else pet} ($150 per flight): {money(amount)}."
    elif action == "loyalty":
        number = re.sub(r"[\s-]", "", v.upper())
        if number != "NONE" and not re.fullmatch(r"[A-Z]{2}\d{6}", number):
            raise ValueError("A Skyway Rewards number is two letters followed by six digits.")
        updated["loyaltyNumber"] = "" if number == "NONE" else number
        summary = "Remove the Skyway Rewards number from this booking." if number == "NONE" else f"Add Skyway Rewards number {number} so this trip earns miles."
    elif action == "cancel_for_credit":
        basis = cancellation_basis(res, action)
        terms = res["cancellation"]
        updated["status"] = "cancelled"
        updated["creditUSD"] += res["paidUSD"] - terms["creditFeeUSD"]
        summary = f"Cancel the entire trip under {basis}. After a ${terms['creditFeeUSD']} cancellation fee, ${updated['creditUSD']} becomes flight credit expiring {terms['creditExpiresOn']}; no cash refund is issued."
    elif action == "cancel_and_refund":
        basis = cancellation_basis(res, action)
        refund = res["paidUSD"]
        updated.update(status="refund_requested", refundRequestedUSD=refund, creditUSD=0)
        summary = f"Cancel the entire trip and submit a refund request for ${refund} to the original form of payment, based on {basis} (card refunds are processed within 7 business days)."
    else:
        raise ValueError("Unknown action.")
    updated["paidUSD"] += amount
    return {"summary": summary, "amountUSD": amount, "next": updated}


def propose(state, action, segment, value, turn=0):
    if not state.get("reservation"):
        raise ValueError("Verify a booking first.")
    if len(state["actions"]) >= 50:
        raise ValueError("Too many changes in one call. Offer a representative.")
    planned = plan(state["reservation"], action, segment, value)
    for p in state["proposals"]:
        if p["status"] == "pending" and p["expiresAt"] > now_ms() and (p["action"], p["segment"], p["value"], p["version"]) == (action, segment, value, state["version"]):
            return p
    for p in state["proposals"]:
        if p["status"] == "pending":
            p["status"] = "superseded"
    p = {"id": str(uuid4()), "action": action, "segment": segment, "value": value, "version": state["version"], "status": "pending",
         "expiresAt": now_ms() + 10 * 60_000, "reference": state["reservation"]["reference"], "summary": planned["summary"], "amountUSD": planned["amountUSD"],
         "createdAfterTurn": turn, "instruction": "NOTHING HAS CHANGED YET. Describe this exact proposal and price, ask whether to proceed, and wait for a NEW caller turn before calling confirm_action."}
    state["proposals"] = state["proposals"][-9:] + [p]
    return p


def decide(state, id, approve):
    p = next((p for p in state["proposals"] if p["id"] == id), None)
    if not p:
        raise ValueError("Unknown proposal.")
    if p["status"] in ("completed", "rejected"):
        return p
    if p["status"] != "pending" or p["reference"] != state["reservation"]["reference"] or p["version"] != state["version"] or now_ms() > p["expiresAt"]:
        raise ValueError("Proposal expired or was superseded. Ask for a new proposal.")
    if not approve:
        p["status"] = "rejected"
        return p
    planned = plan(state["reservation"], p["action"], p["segment"], p["value"])
    if planned["amountUSD"] != p["amountUSD"] or planned["summary"] != p["summary"]:
        raise ValueError("Price or eligibility changed. Propose the action again.")
    state["reservation"] = planned["next"]
    p["status"] = "completed"
    state["version"] += 1
    state["actions"].append({"id": p["id"], "summary": p["summary"], "amountUSD": p["amountUSD"], "result": "Refund request submitted; card refunds are processed within 7 business days." if p["action"] == "cancel_and_refund" else "Completed.", "time": datetime.now(timezone.utc).isoformat(),
                             # Audit: how the caller approved (verified judges or keypad), stored with the booking change.
                             "approval": p.get("approvalEvidence")})
    return p
