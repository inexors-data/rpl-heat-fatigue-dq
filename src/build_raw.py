"""Flatten cached raw JSON into DuckDB tables in the `raw` schema.

Values are kept close to the source (dates stay as source strings); typing and
normalisation happen in the SQL staging layer, where they can be tested.
"""
import json

import duckdb
import pandas as pd

from .common import DB_PATH, RAW_DIR, SEASONS


def _read(source: str, key: str):
    path = RAW_DIR / source / f"{key}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _rpl_tables():
    clubs, matches, events, lineups, stats = [], [], [], [], []
    for tournament in SEASONS:
        for c in _read("rpl", f"clubs_{tournament}") or []:
            clubs.append({"tournament": tournament, "club_id": c["id"], "name": c["name"], "city": c["city"]})
        for stage in _read("rpl", f"matches_{tournament}") or []:
            for m in stage["matches"]:
                if m["status"] != 4:
                    continue
                mid = m["id"]
                full = _read("rpl", f"match_{mid}") or {}
                matches.append({
                    "match_id": mid, "tournament": tournament, "stage_id": stage["stage"],
                    "club_h": m["clubH"], "club_a": m["clubA"], "goal_h": m["goalH"], "goal_a": m["goalA"],
                    "date_raw": m["date"], "city": m["city"], "stadium": m["stadium"],
                    "attendance": full.get("attendance"),
                })
                for e in (_read("rpl", f"events_{mid}") or {}).get("protocol") or []:
                    events.append({
                        "match_id": mid, "event_id": e.get("id"), "type": e.get("type"),
                        "type_extended": e.get("typeExtended"), "minute": e.get("time"), "extra": e.get("extra"),
                        "period": e.get("period"),
                        "player1_id": (e.get("player1") or {}).get("id"),
                        "player2_id": (e.get("player2") or {}).get("id"),
                        "club_id": e.get("club"), "second_yellow": e.get("secondYellow"),
                        "ignore": e.get("ignore"), "custom_html": e.get("htmlCustomEvent"),
                    })
                proto = (_read("rpl", f"lineups_{mid}") or {}).get("protocol") or {}
                for side, club in (("clubH", m["clubH"]), ("clubA", m["clubA"])):
                    for role in ("main", "reserve"):
                        for p in (proto.get(side) or {}).get(role, []):
                            lineups.append({
                                "match_id": mid, "club_id": club, "is_home": side == "clubH",
                                "player_id": p["id"], "is_starter": role == "main",
                                "amplua": p.get("amplua"), "captain": p.get("captain"),
                            })
                for s in (_read("rpl", f"stats_{mid}") or {}).get("match") or []:
                    stats.append({"match_id": mid, "stat_name": s["name"], "home": s["club1"], "away": s["club2"]})

    players = []
    for path in sorted((RAW_DIR / "rpl").glob("player_*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(p, dict) or "id" not in p:
            continue
        players.append({k: p.get(k) for k in (
            "id", "name", "lastname", "nickname", "public_name", "public_lastname",
            "birthday", "height", "weight", "amplua", "citizenship")})

    stadiums = []
    for path in sorted((RAW_DIR / "rpl").glob("stadium_*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(s, dict) and s.get("name"):
            stadiums.append({
                "club_id": int(path.stem.split("_")[1]), "stadium": s["name"], "city": s.get("city"),
                "coords_raw": (s.get("address") or {}).get("coords"),
            })

    return {
        "rpl_clubs": clubs, "rpl_matches": matches, "rpl_events": events, "rpl_lineups": lineups,
        "rpl_team_stats": stats, "rpl_players": players, "rpl_stadiums": stadiums,
    }


def _understat_tables():
    us_matches, shots, rosters = [], [], []
    for year in SEASONS.values():
        league = _read("understat", f"league_{year}")
        if not league:
            continue
        for d in league["dates"]:
            if not d["isResult"]:
                continue
            us_matches.append({
                "us_match_id": int(d["id"]), "season": year,
                "h_id": int(d["h"]["id"]), "h_title": d["h"]["title"],
                "a_id": int(d["a"]["id"]), "a_title": d["a"]["title"],
                "h_goals": d["goals"]["h"], "a_goals": d["goals"]["a"],
                "h_xg": d["xG"]["h"], "a_xg": d["xG"]["a"], "datetime_raw": d["datetime"],
            })
            md = _read("understat", f"match_{d['id']}")
            if not md:
                continue
            for side in ("h", "a"):
                for s in md["shots"][side]:
                    shots.append({
                        "us_match_id": int(d["id"]), "shot_id": s["id"], "h_a": s["h_a"],
                        "minute": s["minute"], "result": s["result"], "xg": s["xG"],
                        "x": s["X"], "y": s["Y"], "situation": s["situation"], "shot_type": s["shotType"],
                        "player_id": s["player_id"], "player": s["player"],
                    })
                for r in md["rosters"][side].values():
                    rosters.append({
                        "us_match_id": int(d["id"]), "h_a": r["h_a"], "us_player_id": r["player_id"],
                        "player": r["player"], "us_team_id": r["team_id"], "minutes": r["time"],
                        "position": r["position"], "roster_in": r["roster_in"], "roster_out": r["roster_out"],
                        "red_card": r["red_card"],
                    })
    return {"us_matches": us_matches, "us_shots": shots, "us_rosters": rosters}


def build(log=print):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    tables = {**_rpl_tables(), **_understat_tables()}
    weather = RAW_DIR / "weather"
    if weather.exists():
        tables["weather_hourly"] = _weather_rows(weather)
    for name, rows in tables.items():
        df = pd.DataFrame(rows)  # noqa: F841  (referenced by name in SQL below)
        con.execute(f"CREATE OR REPLACE TABLE raw.{name} AS SELECT * FROM df")
        log(f"raw.{name}: {len(rows)} rows")
    con.close()


def _weather_rows(folder):
    rows = []
    for path in sorted(folder.glob("*.json")):
        w = json.loads(path.read_text(encoding="utf-8"))
        venue, season = path.stem.rsplit("_", 1)
        h = w["hourly"]
        for i, t in enumerate(h["time"]):
            rows.append({
                "venue": venue, "season": int(season), "time_utc": t,
                "temperature_c": h["temperature_2m"][i], "apparent_temperature_c": h["apparent_temperature"][i],
                "humidity_pct": h["relative_humidity_2m"][i], "precipitation_mm": h["precipitation"][i],
                "wind_kmh": h["wind_speed_10m"][i],
            })
    return rows


if __name__ == "__main__":
    build()
