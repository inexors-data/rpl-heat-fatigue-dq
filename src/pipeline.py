"""End-to-end pipeline: extract -> raw tables -> SQL staging/xref/marts -> DQ report.

Usage:
    python -m src.pipeline            # full run (downloads only what is not cached yet)
    python -m src.pipeline --offline  # rebuild everything from cached raw files
"""
import argparse
import sys

import duckdb

from . import build_raw, dq, load_rpl, load_understat, load_weather
from .common import DB_PATH, ROOT, SQL_DIR


def run_sql():
    con = duckdb.connect(str(DB_PATH))
    # SQL files use repo-relative paths (seeds/...), so resolve them from the project root.
    con.execute(f"SET file_search_path = '{ROOT.as_posix()}'")
    for path in sorted(SQL_DIR.glob("*.sql")):
        print(f"sql: {path.name}")
        con.execute(path.read_text(encoding="utf-8"))
    con.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="skip downloads, use cached raw files")
    args = parser.parse_args()

    if not args.offline:
        load_rpl.load()
        load_understat.load()
        load_weather.load()
    build_raw.build()
    run_sql()
    ok = dq.run()
    print("DQ: OK" if ok else "DQ: errors found, see reports/dq_report.md")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
