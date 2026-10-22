"""Extract from the official RPL site API (api.premierliga.ru) into data/raw/rpl/."""
from .common import SEASONS, cached_json, http_json, run_parallel

API = "https://api.premierliga.ru/api/"


def rpl(endpoint: str, body: dict):
    return http_json("POST", API + endpoint, json=body)


def _load_match(mid: int):
    """Fetch one match; return the player ids it mentions."""
    cached_json("rpl", f"match_{mid}", lambda: rpl("getFullDataMatch", {"id": mid}))
    events = cached_json("rpl", f"events_{mid}", lambda: rpl("getEvents", {"id": mid}))
    lineups = cached_json("rpl", f"lineups_{mid}", lambda: rpl("getLineups", {"id": mid}))
    cached_json("rpl", f"stats_{mid}", lambda: rpl("getGameStatistics", {"id": mid}))
    ids = set()
    for side in ("clubH", "clubA"):
        proto = (lineups.get("protocol") or {}).get(side) or {}
        ids.update(p["id"] for p in proto.get("main", []) + proto.get("reserve", []))
    for e in events.get("protocol") or []:
        for k in ("player1", "player2"):
            if e.get(k):
                ids.add(e[k]["id"])
    return ids


def load(log=print):
    player_ids, club_ids = set(), set()
    for tournament in SEASONS:
        clubs = cached_json("rpl", f"clubs_{tournament}", lambda: rpl("getClubs", {"tournament": tournament}))
        club_ids.update(c["id"] for c in clubs)
        stages = cached_json("rpl", f"matches_{tournament}", lambda: rpl("getMatches", {"tournament": tournament}))
        finished = [m["id"] for s in stages for m in s["matches"] if m["status"] == 4]
        for ids in run_parallel(_load_match, finished):
            player_ids |= ids
        log(f"rpl {tournament}: {len(finished)} finished matches")

    log(f"rpl: {len(club_ids)} clubs, {len(player_ids)} players")
    run_parallel(lambda cid: cached_json("rpl", f"stadium_{cid}", lambda: rpl("getClubStadium", {"id": cid})),
                 sorted(club_ids))
    run_parallel(lambda pid: cached_json("rpl", f"player_{pid}", lambda: rpl("getPlayerProfile", {"id": pid})),
                 sorted(player_ids))


if __name__ == "__main__":
    load()
