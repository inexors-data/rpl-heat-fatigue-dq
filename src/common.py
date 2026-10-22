"""Shared settings and an HTTP client with an on-disk cache of raw responses."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import truststore

# Use the OS certificate store (corporate proxies re-sign TLS; certifi alone rejects it).
truststore.inject_into_ssl()
import requests  # noqa: E402  (must be imported after truststore injection)

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
DB_PATH = ROOT / "data" / "dynamo.duckdb"
SQL_DIR = ROOT / "sql"
REPORTS_DIR = ROOT / "reports"

# Seasons in scope: RPL tournament id -> Understat season (start year).
SEASONS = {719: 2023, 721: 2024, 722: 2025}

_session = requests.Session()
_session.headers["User-Agent"] = "dynamo-petproject/0.1 (football analytics research)"


def cached_json(source: str, key: str, fetch, refresh: bool = False):
    """Return JSON for (source, key); call fetch() and store it only on cache miss.

    Raw responses are kept verbatim so that every downstream table can be rebuilt
    offline and any parsing bug can be traced back to the original payload.
    """
    path = RAW_DIR / source / f"{key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted run never leaves a truncated cache file.
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return data


def run_parallel(func, items, workers: int = 4):
    """Apply func to items with a small thread pool (I/O bound, polite concurrency)."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(func, items))


def http_json(method: str, url: str, *, pause: float = 0.25, retries: int = 3, **kwargs):
    """HTTP call with polite pacing and simple exponential backoff."""
    for attempt in range(retries):
        try:
            resp = _session.request(method, url, timeout=60, **kwargs)
            resp.raise_for_status()
            time.sleep(pause)
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
