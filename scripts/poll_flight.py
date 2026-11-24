import json
import math
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEX = "a1324d"
FLIGHTS_PATH = Path("data/flights.json")
AIRPORTS_PATH = Path("data/airports.json")
LANDING_GAP_MIN = 15


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "WheresTaylor-Logger/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_aircraft():
    for url, key in [
        (f"https://api.adsb.lol/v2/hex/{HEX}", "ac"),
        (f"https://opendata.adsb.fi/api/v2/icao/{HEX}", "aircraft"),
    ]:
        try:
            data = fetch_json(url)
            rows = data.get(key) or data.get("ac") or []
            return rows[0] if rows else None
        except Exception:
            continue
    return None


def haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_airport(lat, lon, airports):
    best = min(airports, key=lambda a: haversine_nm(lat, lon, a["lat"], a["lon"]))
    return best["code"]


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main():
    data = json.loads(FLIGHTS_PATH.read_text())
    airports = json.loads(AIRPORTS_PATH.read_text())
    state = data["state"]

    ac = fetch_aircraft()
    now_str = iso_now()
    now_dt = parse_iso(now_str)

    is_airborne = bool(
        ac
        and ac.get("lat") is not None
        and ac.get("lon") is not None
        and ac.get("alt_baro") != "ground"
    )

    if is_airborne:
        lat, lon = round(float(ac["lat"]), 4), round(float(ac["lon"]), 4)
        if not state.get("airborne"):
            state["airborne"] = True
            state["takeoff_utc"] = now_str
            state["origin"] = state.get("last_airport") or nearest_airport(lat, lon, airports)
            print(f"Takeoff detected from {state['origin']} at {now_str}")
        state["last_seen_utc"] = now_str
        state["last_lat"] = lat
        state["last_lon"] = lon
        FLIGHTS_PATH.write_text(json.dumps(data, indent=2) + "\n")
        return

    if state.get("airborne"):
        last_dt = parse_iso(state["last_seen_utc"])
        gap_min = (now_dt - last_dt).total_seconds() / 60
        if gap_min >= LANDING_GAP_MIN:
            dest = nearest_airport(state["last_lat"], state["last_lon"], airports)
            takeoff_str = state.get("takeoff_utc", state["last_seen_utc"])
            landing_str = state["last_seen_utc"]
            duration_min = max(
                5, round((parse_iso(landing_str) - parse_iso(takeoff_str)).total_seconds() / 60)
            )
            origin = state.get("origin") or state.get("last_airport") or dest
            flight_id = f"{takeoff_str[:16].replace(':', '')}Z-{origin}-{dest}"

            if not any(f["id"] == flight_id for f in data["flights"]):
                data["flights"].insert(
                    0,
                    {
                        "id": flight_id,
                        "origin": origin,
                        "destination": dest,
                        "takeoff_utc": takeoff_str,
                        "landing_utc": landing_str,
                        "duration_min": duration_min,
                    },
                )
            state["airborne"] = False
            state["last_airport"] = dest
            state.pop("takeoff_utc", None)
            state.pop("origin", None)
            FLIGHTS_PATH.write_text(json.dumps(data, indent=2) + "\n")
            print(f"Logged flight {origin} -> {dest} ({duration_min} min)")
        else:
            print(f"No ping for {gap_min:.1f} min — waiting for {LANDING_GAP_MIN} min landing buffer.")
    else:
        print("N1763W not airborne — no changes.")


if __name__ == "__main__":
    main()