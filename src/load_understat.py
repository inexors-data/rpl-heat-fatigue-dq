"""Extract RFPL shots and rosters from Understat into data/raw/understat/."""
from .common import SEASONS, cached_json, http_json

BASE = "https://understat.com/"
HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def understat(path: str):
    return http_json("GET", BASE + path, headers=HEADERS, pause=0.4)


def load(log=print):
    for year in SEASONS.values():
        league = cached_json("understat", f"league_{year}", lambda: understat(f"getLeagueData/RFPL/{year}"))
        finished = [d for d in league["dates"] if d["isResult"]]
        log(f"understat {year}: {len(finished)} finished matches")
        for d in finished:
            cached_json("understat", f"match_{d['id']}", lambda: understat(f"getMatchData/{d['id']}"))


if __name__ == "__main__":
    load()
