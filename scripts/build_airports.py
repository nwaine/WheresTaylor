import csv
import io
import json
import urllib.request
from pathlib import Path

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
REGIONS = {"US-WA", "US-OR", "US-CA", "US-ID", "US-MT", "US-NV", "US-AZ", "US-UT", "CA-BC"}
TYPES = {"small_airport", "medium_airport", "large_airport", "seaplane_base"}

def main():
    print(f"Downloading {OURAIRPORTS_URL} ...")
    with urllib.request.urlopen(OURAIRPORTS_URL, timeout=30) as resp:
        text = resp.read().decode("utf-8")

    airports = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["iso_region"] not in REGIONS or row["type"] not in TYPES:
            continue
        code = row["local_code"] or row["ident"]
        airports.append({
            "code": code,
            "ident": row["ident"],
            "name": row["name"],
            "city": row["municipality"],
            "region": row["iso_region"].split("-")[-1],
            "lat": round(float(row["latitude_deg"]), 5),
            "lon": round(float(row["longitude_deg"]), 5),
        })

    out = Path("data/airports.json")
    out.write_text(json.dumps(airports, separators=(",", ":")))
    print(f"Saved {len(airports)} airports to {out} ({out.stat().st_size / 1024:.1f} KB)")

if __name__ == "__main__":
    main()