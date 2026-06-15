"""
migrate.py — One-shot migration of local SQLite data to Railway PostgreSQL.

When to use:
    Your Flask app on Railway is up and running, but the PostgreSQL DB is
    empty so "Top players" / "Similar matches" sections are blank.
    Run this once locally to copy the worldcup.db data to Railway.

How to use:
    1. Railway dashboard -> Postgres service -> Settings -> Networking
       -> Enable "Public Networking". A public URL appears.
    2. Copy the DATABASE_URL (looks like `postgres://user:pass@host:port/db`).
    3. In PowerShell (project folder):
           $env:DATABASE_URL = "postgres://..."
           python migrate.py
       Or on macOS/Linux:
           DATABASE_URL="postgres://..." python migrate.py
    4. Wait. Typical time over residential internet: 3-8 minutes.
    5. Refresh the Railway URL — "주목할 선수 TOP 3" / "유사 과거 경기"
       should now have data.
    6. (Recommended security) Disable Public Networking afterwards so the
       PG endpoint isn't exposed to the internet anymore.

Idempotent: safe to re-run. INSERT ... ON CONFLICT DO NOTHING skips dupes.
Auto-increment id columns are intentionally omitted; PG sequences assign them.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    print("ERROR: psycopg2 is required.  pip install psycopg2-binary")
    sys.exit(1)


SQLITE_PATH = Path(__file__).parent / "worldcup.db"
BATCH_SIZE = 1000


# Tables in foreign-key-safe insertion order.
# Auto-increment id columns (match_shots, match_events) are omitted on
# purpose so PostgreSQL assigns fresh sequence values.
TABLES: list[tuple[str, list[str]]] = [
    ("teams",          ["id", "name", "abbreviation", "country_code"]),
    ("players",        ["id", "name", "team_id"]),
    ("matches",        ["id", "season", "stage", "group_name",
                        "home_team_id", "away_team_id",
                        "home_score", "away_score", "kickoff_utc", "status",
                        "home_formation", "away_formation"]),
    ("team_stats",     ["match_id", "team_id", "is_home", "possession",
                        "shots_total", "shots_on_target",
                        "xg_total", "xgot_total", "corners", "fouls"]),
    ("player_stats",   ["match_id", "player_id", "team_id", "minutes_played",
                        "expected_goals", "expected_assists",
                        "shots", "passes_accurate", "rating"]),
    ("match_shots",    ["match_id", "player_id", "team_id", "minute",
                        "xg", "xgot", "player_x", "player_y",
                        "body_part", "situation", "shot_type"]),
    ("match_momentum", ["match_id", "minute", "home_momentum", "away_momentum"]),
    ("match_events",   ["match_id", "minute", "event_type", "team_id", "player_id"]),
]


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("DATABASE_URL is not set.")
        print()
        print("Steps to get it:")
        print("  1. Railway dashboard -> your Postgres service")
        print("  2. Settings -> Networking -> 'Enable Public Networking'")
        print("  3. Copy the public DATABASE_URL")
        print("  4. PowerShell:  $env:DATABASE_URL = '<paste>'")
        print("  5. Re-run: python migrate.py")
        return 2

    if not SQLITE_PATH.exists():
        print(f"Source SQLite not found at {SQLITE_PATH}.")
        print("Run `python data_pipeline.py backfill` first to populate it.")
        return 3

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    host_part = db_url.split("@", 1)[-1][:60] if "@" in db_url else "env"
    print(f"Source:  {SQLITE_PATH}")
    print(f"Target:  PostgreSQL  ({host_part})")
    print()

    src = sqlite3.connect(str(SQLITE_PATH))
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(db_url)
    cur = dst.cursor()

    try:
        total_inserted = 0
        for table, cols in TABLES:
            col_list = ", ".join(cols)
            rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
            n = len(rows)
            if n == 0:
                print(f"  {table:18s}  0 rows                  (skipped)")
                continue

            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES %s "
                "ON CONFLICT DO NOTHING"
            )
            inserted = 0
            for i in range(0, n, BATCH_SIZE):
                batch = rows[i:i + BATCH_SIZE]
                values = [tuple(r[c] for c in cols) for r in batch]
                execute_values(cur, sql, values, page_size=BATCH_SIZE)
                inserted += len(batch)
                print(f"  {table:18s}  {inserted}/{n}", end="\r", flush=True)
            dst.commit()
            print(f"  {table:18s}  {inserted}/{n}    done            ")
            total_inserted += inserted

        print()
        print(f"Migration complete. {total_inserted} rows copied.")
        print()
        print("Now refresh the Railway URL — top players and similar matches")
        print("should appear. Recommended next step: disable Public Networking")
        print("on the Railway Postgres service.")
        return 0
    finally:
        cur.close()
        dst.close()
        src.close()


if __name__ == "__main__":
    sys.exit(main())
