const HEX = "a1324d";

// Airports without their own ASOS/AWOS fall back to the closest nearby METAR station
const METAR_MAP = {
  S50: { station: "KRNT", note: "Renton, 7 nm N" },
  KS50: { station: "KRNT", note: "Renton, 7 nm N" },
};

async function fetchJson(url, timeoutMs = 5000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      headers: { "User-Agent": "WheresTaylor/1.0" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchLiveAircraft() {
  try {
    const data = await fetchJson(`https://api.adsb.lol/v2/hex/${HEX}`);
    const ac = (data.ac || [])[0] || null;
    return { source: "adsb.lol", ac };
  } catch {
    try {
      const data = await fetchJson(`https://opendata.adsb.fi/api/v2/icao/${HEX}`);
      const ac = (data.aircraft || data.ac || [])[0] || null;
      return { source: "adsb.fi", ac };
    } catch {
      return { source: "unavailable", ac: null };
    }
  }
}

async function fetchMetar(airportCode) {
  const code = (airportCode || "S50").toUpperCase();
  const mapped = METAR_MAP[code] || {
    station: code.length === 3 ? `K${code}` : code,
    note: null,
  };
  try {
    const rows = await fetchJson(
      `https://aviationweather.gov/api/data/metar?ids=${encodeURIComponent(mapped.station)}&format=json`
    );
    const m = Array.isArray(rows) ? rows[0] : null;
    if (!m) return null;
    const tempF = typeof m.temp === "number" ? Math.round((m.temp * 9) / 5 + 32) : null;
    const wind =
      m.wspd > 0
        ? `${m.wdir === "VRB" ? "VRB" : String(m.wdir ?? "").padStart(3, "0")}° @ ${m.wspd} kt${m.wgst ? ` G${m.wgst}` : ""}`
        : "Calm";
    return {
      station: mapped.station,
      note: mapped.note,
      fltCat: m.fltCat || "VFR",
      tempF,
      wind,
      raw: m.rawOb || "",
    };
  } catch {
    return null;
  }
}

exports.handler = async (event) => {
  const airport = (event.queryStringParameters && event.queryStringParameters.airport) || "S50";
  const [{ source, ac }, metar] = await Promise.all([
    fetchLiveAircraft(),
    fetchMetar(airport),
  ]);

  const live = ac
    ? {
        airborne: ac.alt_baro !== "ground",
        lat: ac.lat ?? null,
        lon: ac.lon ?? null,
        alt_ft: typeof ac.alt_baro === "number" ? ac.alt_baro : null,
        gs_kt: typeof ac.gs === "number" ? Math.round(ac.gs) : null,
        track: typeof ac.track === "number" ? Math.round(ac.track) : null,
        vrate_fpm: typeof ac.baro_rate === "number" ? ac.baro_rate : null,
        squawk: ac.squawk || null,
        callsign: (ac.flight || "N1763W").trim(),
      }
    : { airborne: false };

  return {
    statusCode: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=30, s-maxage=60",
    },
    body: JSON.stringify({
      checked_utc: new Date().toISOString(),
      source,
      live,
      metar,
    }),
  };
};