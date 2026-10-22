"""Extract hourly historical weather per venue and season from Open-Meteo."""
import csv
import json
from datetime import date, timedelta

from .common import RAW_DIR, ROOT, SEASONS, cached_json, http_json

URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,wind_speed_10m"


def venues():
    with open(ROOT / "seeds" / "venues.csv", encoding="utf-8") as f:
        return {row["stadium"]: row for row in csv.DictReader(f)}


def _match_dates(tournament):
    """Dates of finished matches per stadium name. Only the date part is used here,
    so the time-zone label of the RPL timestamp does not matter (±1 day padding)."""
    stages = json.loads((RAW_DIR / "rpl" / f"matches_{tournament}.json").read_text(encoding="utf-8"))
    out = {}
    for s in stages:
        for m in s["matches"]:
            if m["status"] == 4:
                out.setdefault(m["stadium"], []).append(date.fromisoformat(m["date"][:10]))
    return out


def load(log=print):
    known = venues()
    for tournament, season in SEASONS.items():
        for stadium, dates in _match_dates(tournament).items():
            v = known.get(stadium)
            if v is None:
                log(f"weather: no coordinates for venue {stadium!r} - add it to seeds/venues.csv")
                continue
            start, end = min(dates) - timedelta(days=1), max(dates) + timedelta(days=1)
            params = {
                "latitude": v["lat"], "longitude": v["lon"], "timezone": "UTC", "hourly": HOURLY,
                "start_date": start.isoformat(), "end_date": end.isoformat(),
            }
            cached_json("weather", f"{v['venue_key']}_{season}", lambda: http_json("GET", URL, params=params))
        log(f"weather {season}: done")


if __name__ == "__main__":
    load()
