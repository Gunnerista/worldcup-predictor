"""
database.py
===========

SQLite schema + connection helpers for worldcup-predictor.

DB location: ./worldcup.db (relative to project root, gitignored via *.db)

Run directly to create the schema:
    python database.py

------------------------------------------------------------------
Project-wide invariants (referenced by data_pipeline.py + others)
------------------------------------------------------------------

  1. BALLDONTLIE filter parameter MUST be `match_ids[]=...` (array form).
     The form `match_id=...` is SILENTLY IGNORED by the API and returns
     the default unfiltered page. See MATCH_FILTER_KEY below.

  2. All BALLDONTLIE calls MUST throttle. Use API_THROTTLE_SECONDS as the
     minimum delay between successive requests. GOAT plan = 600/min, but
     short bursts hit 429.

  3. Player names: never fetch by player_id one-at-a-time over HTTP.
     Backfill the `players` table once, then resolve via get_player_name().

------------------------------------------------------------------
API -> schema field-name mappings (non-obvious only)
------------------------------------------------------------------

  team_stats.possession      <- team_match_stats.possession_pct
  team_stats.xg_total        <- team_match_stats.expected_goals
  team_stats.xgot_total      <- DERIVED: SUM(match_shots.xgot) per (match, team)
  player_stats.shots         <- DERIVED: COUNT(match_shots) per (match, player)
                                (player_match_stats has no raw shot count)
  match_momentum.{home,away}_momentum
                             <- match_momentum.value (signed integer):
                                  value >= 0  =>  home_momentum=value,  away_momentum=0
                                  value <  0  =>  home_momentum=0,      away_momentum=-value
                                Lossless: original = home_momentum - away_momentum.
  players.team_id            <- DERIVED: join /players.country_code to teams.country_code
                                (the /players endpoint does NOT return team_id directly,
                                 because club affiliation differs from national team).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants -- imported by other modules. Single source of truth.
# ---------------------------------------------------------------------------

DB_PATH: Path = Path(__file__).parent / "worldcup.db"

API_THROTTLE_SECONDS: float = 0.3   # 200 req/min — well under 600 cap and avoids burst-limit 429s
MATCH_FILTER_KEY: str = "match_ids[]"   # NOT "match_id="
BALLDONTLIE_BASE: str = "https://api.balldontlie.io/fifa/worldcup/v1"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS teams (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        abbreviation  TEXT,
        country_code  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS players (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        team_id   INTEGER,
        FOREIGN KEY (team_id) REFERENCES teams(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS matches (
        id              INTEGER PRIMARY KEY,
        season          INTEGER NOT NULL,
        stage           TEXT,
        group_name      TEXT,
        home_team_id    INTEGER NOT NULL,
        away_team_id    INTEGER NOT NULL,
        home_score      INTEGER,
        away_score      INTEGER,
        kickoff_utc     TEXT,
        status          TEXT,
        home_formation  TEXT,
        away_formation  TEXT,
        FOREIGN KEY (home_team_id) REFERENCES teams(id),
        FOREIGN KEY (away_team_id) REFERENCES teams(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_stats (
        match_id         INTEGER NOT NULL,
        team_id          INTEGER NOT NULL,
        is_home          INTEGER NOT NULL,
        possession       REAL,
        shots_total      INTEGER,
        shots_on_target  INTEGER,
        xg_total         REAL,
        xgot_total       REAL,
        corners          INTEGER,
        fouls            INTEGER,
        PRIMARY KEY (match_id, team_id),
        FOREIGN KEY (match_id) REFERENCES matches(id),
        FOREIGN KEY (team_id)  REFERENCES teams(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_stats (
        match_id          INTEGER NOT NULL,
        player_id         INTEGER NOT NULL,
        team_id           INTEGER NOT NULL,
        minutes_played    INTEGER,
        expected_goals    REAL,
        expected_assists  REAL,
        shots             INTEGER,
        passes_accurate   INTEGER,
        rating            REAL,
        PRIMARY KEY (match_id, player_id),
        FOREIGN KEY (match_id)  REFERENCES matches(id),
        FOREIGN KEY (player_id) REFERENCES players(id),
        FOREIGN KEY (team_id)   REFERENCES teams(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_shots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id    INTEGER NOT NULL,
        player_id   INTEGER,
        team_id     INTEGER NOT NULL,
        minute      INTEGER,
        xg          REAL,
        xgot        REAL,
        player_x    REAL,
        player_y    REAL,
        body_part   TEXT,
        situation   TEXT,
        shot_type   TEXT,
        FOREIGN KEY (match_id)  REFERENCES matches(id),
        FOREIGN KEY (player_id) REFERENCES players(id),
        FOREIGN KEY (team_id)   REFERENCES teams(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_momentum (
        match_id        INTEGER NOT NULL,
        minute          INTEGER NOT NULL,
        home_momentum   REAL,
        away_momentum   REAL,
        PRIMARY KEY (match_id, minute),
        FOREIGN KEY (match_id) REFERENCES matches(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id    INTEGER NOT NULL,
        minute      INTEGER,
        event_type  TEXT NOT NULL,
        team_id     INTEGER,
        player_id   INTEGER,
        FOREIGN KEY (match_id)  REFERENCES matches(id),
        FOREIGN KEY (team_id)   REFERENCES teams(id),
        FOREIGN KEY (player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS predictions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id        INTEGER NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        home_win_pct    REAL NOT NULL,
        draw_pct        REAL NOT NULL,
        away_win_pct    REAL NOT NULL,
        model_version   TEXT,
        notes           TEXT,
        FOREIGN KEY (match_id) REFERENCES matches(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_notes (
        match_id          INTEGER NOT NULL,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        tactics           TEXT,
        player_condition  TEXT,
        psychology        TEXT,
        other             TEXT,
        PRIMARY KEY (match_id, created_at),
        FOREIGN KEY (match_id) REFERENCES matches(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS polymarket_odds (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id      INTEGER,
        market_type   TEXT NOT NULL,
        team_name     TEXT,
        probability   REAL NOT NULL,
        captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (match_id) REFERENCES matches(id)
    )
    """,
]

INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_matches_season       ON matches(season)",
    "CREATE INDEX IF NOT EXISTS idx_matches_kickoff      ON matches(kickoff_utc)",
    "CREATE INDEX IF NOT EXISTS idx_match_shots_match    ON match_shots(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_match_events_match   ON match_events(match_id)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_match    ON predictions(match_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_market    ON polymarket_odds(market_type, captured_at)",
]


# ---------------------------------------------------------------------------
# Connection + schema setup
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open SQLite with FK constraints ON and Row factory for dict-like access."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables and indexes. Idempotent (uses IF NOT EXISTS everywhere)."""
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        for stmt in INDEXES:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Player name resolution (in-process cache backed by `players` table)
# ---------------------------------------------------------------------------

_player_name_cache: dict[int, Optional[str]] = {}


def get_player_name(conn: sqlite3.Connection, player_id: int) -> Optional[str]:
    """Resolve player_id -> name. Caches both hits and misses for the process lifetime.

    Reason: BALLDONTLIE player_match_stats and match_shots responses include
    player_id but NOT name. We backfill the `players` table once via
    data_pipeline.py, then resolve names locally instead of per-row HTTP.
    """
    if player_id in _player_name_cache:
        return _player_name_cache[player_id]
    row = conn.execute("SELECT name FROM players WHERE id = ?", (player_id,)).fetchone()
    name = row["name"] if row else None
    _player_name_cache[player_id] = name
    return name


def clear_player_cache() -> None:
    """Drop the in-memory player cache. Call after a bulk re-backfill."""
    _player_name_cache.clear()


# ---------------------------------------------------------------------------
# Run-as-script: build the schema, report what's there
# ---------------------------------------------------------------------------

def _report(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        print(f"DB:      {db_path}")
        print(f"tables:  {len(tables)}")
        for t in tables:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t['name']}").fetchone()[0]
            print(f"  {t['name']:20s}  rows={cnt}")

        idxs = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_%' ORDER BY name"
        ).fetchall()
        print(f"indexes: {len(idxs)}")
        for i in idxs:
            print(f"  {i['name']}")

        fk_check = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        print(f"foreign_keys pragma: {'ON' if fk_check else 'OFF'}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    _report(DB_PATH)
