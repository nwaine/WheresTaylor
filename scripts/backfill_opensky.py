#!/usr/bin/env python3
"""
Backfill historical flights for N1763W (ICAO hex a1324d) from the OpenSky Network API
into data/flights.json.

Setup (one time, free):
  1. Create a free account at https://opensky-network.org/
  2. Go to Account -> API Client -> Create new API client (downloads credentials.json)
  3. Run from your WheresTaylor repo folder:
       python3 scripts/backfill_opensky.py --credentials ~/Downloads/credentials.json --days 180

Notes:
  - OpenSky requires OAuth2 client credentials (since March 2026) and limits
    /api/flights/aircraft to 2-day windows.
  - A free OpenSky account gets 4,000 credits/day (~30 credits per 2-day window,
    so ~180 days / 90 windows fits comfortably inside one day's free quota).
  - Deduplicates against existing flights in data/flights.json (within 15 min)
    and saves progress cleanly if you hit a rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ICAO24 = "a1324d"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
REPO_ROOT = Path(__file__).resolve().parent.parent
FLIGHTS_PATH = REPO_ROOT / "data" / "flights.json"
AIRPORTS_PATH = REPO_ROOT / "data" / "airports.json"

# Map OurAirports/OpenSky 4-letter codes back to familiar local codes where preferred
CODE_ALIASES = {
    "KS50": "S50",
}


def load_credentials(cred_path: str | None) -> tuple[str, str]:
    if cred_path:
        raw = json.loads(Path(cred_path).expanduser().read_text())
        cid = raw.get("clientId") or raw.get("client_id")
        csec = raw.get("clientSecret") or raw.get("client_secret")
        if cid and csec:
            return cid.strip(), csec.strip()
        raise SystemExit(f"Could not find clientId/clientSecret in {cred_path}")

    cid = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    csec = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if cid and csec:
        return cid, csec

    raise SystemExit(
        "Missing OpenSky credentials. Pass --credentials ~/Downloads/credentials.json "
        "or set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET."
    )


def fetch_token(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Token response missing access_token: {payload}")
    return token


def normalize_airport(code: str | None, known_codes: set[str]) -> str | None:
    if not code:
        return None
    c = code.strip().upper()
    if c in CODE_ALIASES:
        return CODE_ALIASES[c]
    if c in known_codes:
        return c
    if c.startswith("K") and len(c) == 4 and c[1:] in known_codes:
        return c[1:]
    return c


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fetch_window(token: str, begin: int, end: int) -> tuple[list[dict], int | None]:
    qs = urllib.parse.urlencode({"icao24": ICAO24, "begin": begin, "end": end})
    req = urllib.request.Request(
        f"{FLIGHTS_URL}?{qs}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "WheresTaylor-Backfill/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            remaining = resp.headers.get("X-Rate-Limit-Remaining")
            rem_int = int(remaining) if remaining and remaining.isdigit() else None
            return json.loads(resp.read().decode("utf-8")), rem_int
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # OpenSky returns 404 when no flights exist in the window
            remaining = e.headers.get("X-Rate-Limit-Remaining") if e.headers else None
            rem_int = int(remaining) if remaining and remaining.isdigit() else None
            return [], rem_int
        raise


def merge_flights(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    merged = list(existing)
    existing_starts = [parse_iso(f["takeoff_utc"]) for f in merged]
    added = 0

    for f in incoming:
        t0 = parse_iso(f["takeoff_utc"])
        # Deduplicate against any flight taking off within 15 minutes
        if any(abs(t0 - ex) <= 900 for ex in existing_starts):
            continue
        merged.append(f)
        existing_starts.append(t0)
        added += 1

    merged.sort(key=lambda x: x["takeoff_utc"], reverse=True)
    return merged, added


def save_flights(data: dict) -> None:
    FLIGHTS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill N1763W flights from OpenSky")
    parser.add_argument(
        "--credentials",
        help="Path to credentials.json downloaded from OpenSky Account -> API Client",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="How many days back to scan (default: 180)",
    )
    parser.add_argument(
        "--end",
        help="End date YYYY-MM-DD (UTC, exclusive; default: start of today UTC)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Seconds to pause between API windows (default: 0.4)",
    )
    args = parser.parse_args()

    if not FLIGHTS_PATH.exists():
        raise SystemExit(f"Could not find {FLIGHTS_PATH}. Run this inside your WheresTaylor repo.")

    known_codes: set[str] = set()
    if AIRPORTS_PATH.exists():
        for a in json.loads(AIRPORTS_PATH.read_text(encoding="utf-8")):
            if a.get("code"):
                known_codes.add(a["code"].upper())
            if a.get("ident"):
                known_codes.add(a["ident"].upper())

    client_id, client_secret = load_credentials(args.credentials)
    print("Authenticating with OpenSky OAuth2...")
    token = fetch_token(client_id, client_secret)
    token_time = time.monotonic()

    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        # OpenSky batches historical flights overnight, so stop at start of today UTC
        now_utc = datetime.now(timezone.utc)
        end_dt = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    start_dt = end_dt - timedelta(days=args.days)
    data = json.loads(FLIGHTS_PATH.read_text(encoding="utf-8"))
    found_raw: list[dict] = []

    cursor = end_dt
    window_num = 0
    total_windows = (args.days + 1) // 2
    print(f"Scanning {ICAO24} from {start_dt.date()} to {end_dt.date()} in 2-day windows (~{total_windows} requests)...")

    while cursor > start_dt:
        # Refresh token before 30-min expiry
        if time.monotonic() - token_time > 1200:
            token = fetch_token(client_id, client_secret)
            token_time = time.monotonic()

        win_begin_dt = max(start_dt, cursor - timedelta(days=2))
        begin_ts = int(win_begin_dt.timestamp())
        end_ts = int(cursor.timestamp()) - 1
        window_num += 1

        try:
            rows, remaining = fetch_window(token, begin_ts, end_ts)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                token = fetch_token(client_id, client_secret)
                token_time = time.monotonic()
                rows, remaining = fetch_window(token, begin_ts, end_ts)
            elif e.code == 429:
                print(
                    f"\nHit OpenSky daily rate limit at {cursor.date()}. "
                    f"Saving {len(found_raw)} flights found so far..."
                )
                print(f"Resume tomorrow with: --end {cursor.strftime('%Y-%m-%d')}")
                break
            else:
                print(f"\nHTTP {e.code} on {win_begin_dt.date()}..{cursor.date()}: {e.reason}")
                cursor = win_begin_dt
                time.sleep(args.delay)
                continue
        except Exception as e:
            print(f"\nError on {win_begin_dt.date()}..{cursor.date()}: {e}")
            cursor = win_begin_dt
            time.sleep(args.delay)
            continue

        for r in rows:
            first_seen = r.get("firstSeen")
            last_seen = r.get("lastSeen")
            if not first_seen or not last_seen or last_seen <= first_seen:
                continue
            origin = normalize_airport(r.get("estDepartureAirport"), known_codes)
            dest = normalize_airport(r.get("estArrivalAirport"), known_codes)
            if not origin or not dest:
                # Skip fragments where OpenSky couldn't match departure/arrival airports
                continue
            dur_min = max(1, round((last_seen - first_seen) / 60))
            if dur_min < 3:
                # Skip tiny taxi/run-up blips
                continue
            takeoff_str = iso_utc(first_seen)
            landing_str = iso_utc(last_seen)
            fid = f"{takeoff_str[:16].replace(':', '')}Z-{origin}-{dest}"
            found_raw.append(
                {
                    "id": fid,
                    "origin": origin,
                    "destination": dest,
                    "takeoff_utc": takeoff_str,
                    "landing_utc": landing_str,
                    "duration_min": dur_min,
                }
            )

        if rows:
            print(
                f"  [{window_num}/{total_windows}] {win_begin_dt.date()} -> {cursor.date()}: "
                f"+{len(rows)} flight(s)" + (f" (credits left: {remaining})" if remaining is not None else "")
            )
        elif window_num % 10 == 0:
            print(
                f"  [{window_num}/{total_windows}] scanned back to {win_begin_dt.date()}..."
                + (f" (credits left: {remaining})" if remaining is not None else "")
            )

        cursor = win_begin_dt
        time.sleep(args.delay)

    merged, added = merge_flights(data.get("flights", []), found_raw)
    data["flights"] = merged
    save_flights(data)

    earliest = merged[-1]["takeoff_utc"][:10] if merged else "n/a"
    print(f"\nDone! Added {added} new flight(s). Total in data/flights.json: {len(merged)} (earliest: {earliest}).")


if __name__ == "__main__":
    main()
