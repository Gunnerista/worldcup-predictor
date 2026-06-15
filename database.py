"""
database.py
===========

Dual-backend storage layer.

Backend selection:
  * If `DATABASE_URL` env var is set (Railway, Heroku, etc.) -> PostgreSQL
  * Otherwise -> SQLite at ./worldcup.db (local dev)

The PostgreSQL path includes a compatibility wrapper so EXISTING SQLite-style
SQL across app.py / data_pipeline.py keeps working unchanged:

    SQL surface                       Translation applied on PostgreSQL
    --------------------------------- --------------------------------------
    `?` placeholders                  -> `%s`
    `INSERT OR IGNORE INTO foo ...`   -> `INSERT INTO foo ... ON CONFLICT DO NOTHING`
    Row access `row["col"]`           -> works via _RowProxy
    Row access `row[0]`               -> works via _RowProxy
    `conn.execute(...)` shortcut      -> works via _PgConnection.execute
    `cur.fetchone()` / `.fetchall()`  -> returns _RowProxy objects

Run directly to create / inspect the schema:
    python database.py

------------------------------------------------------------------
Project-wide invariants (referenced by data_pipeline.py + others)
------------------------------------------------------------------

  1. BALLDONTLIE filter parameter MUST be `match_ids[]=...` (array form).
  2. All BALLDONTLIE calls MUST throttle. Use API_THROTTLE_SECONDS.
  3. Player names: never fetch by player_id one-at-a-time over HTTP.

------------------------------------------------------------------
API -> schema field-name mappings (non-obvious only)
------------------------------------------------------------------

  team_stats.possession      <- team_match_stats.possession_pct
  team_stats.xg_total        <- team_match_stats.expected_goals
  team_stats.xgot_total      <- DERIVED: SUM(match_shots.xgot) per (match, team)
  player_stats.shots         <- DERIVED: COUNT(match_shots) per (match, player)
  match_momentum.{home,away}_momentum
                             <- match_momentum.value (signed):
                                  v >= 0 -> home_momentum=v, away_momentum=0
                                  v <  0 -> home_momentum=0, away_momentum=-v
  players.team_id            <- DERIVED: country_code join to teams.country_code
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES: bool = bool(DATABASE_URL)

# Lazy import psycopg2 only if we actually need PostgreSQL.
# Falling back to SQLite if psycopg2 is missing keeps local dev frictionless.
psycopg2 = None
if USE_POSTGRES:
    try:
        import psycopg2 as _pg
        psycopg2 = _pg
    except ImportError:
        USE_POSTGRES = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH: Path = Path(__file__).parent / "worldcup.db"

API_THROTTLE_SECONDS: float = 0.3
MATCH_FILTER_KEY: str = "match_ids[]"   # NOT "match_id="
BALLDONTLIE_BASE: str = "https://api.balldontlie.io/fifa/worldcup/v1"


# ---------------------------------------------------------------------------
# Schema (portable to SQLite + PostgreSQL)
# ---------------------------------------------------------------------------
#
# Type mapping notes:
#   * INTEGER PRIMARY KEY (no AUTOINCREMENT) is portable: in SQLite it's a
#     rowid alias, in PostgreSQL it's INT NOT NULL PRIMARY KEY with caller
#     providing IDs. All our `teams`/`players`/`matches` rows come from
#     BALLDONTLIE with explicit ids, so no auto-gen needed.
#   * For auto-gen tables (match_shots etc.) we branch on USE_POSTGRES.
#   * Timestamp defaults use `CURRENT_TIMESTAMP` which is supported by both.
#     SQLite stores it as a text/numeric value; PostgreSQL stores timestamptz.
#     Column type `TIMESTAMP` is accepted by SQLite (NUMERIC affinity).

def _schema_statements(is_pg: bool) -> list[str]:
    auto_pk = "BIGSERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return [
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
        f"""
        CREATE TABLE IF NOT EXISTS match_shots (
            id          {auto_pk},
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
        f"""
        CREATE TABLE IF NOT EXISTS match_events (
            id          {auto_pk},
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
        f"""
        CREATE TABLE IF NOT EXISTS predictions (
            id              {auto_pk},
            match_id        INTEGER NOT NULL,
            created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
            created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            tactics           TEXT,
            player_condition  TEXT,
            psychology        TEXT,
            other             TEXT,
            PRIMARY KEY (match_id, created_at),
            FOREIGN KEY (match_id) REFERENCES matches(id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS polymarket_odds (
            id            {auto_pk},
            match_id      INTEGER,
            market_type   TEXT NOT NULL,
            team_name     TEXT,
            probability   REAL NOT NULL,
            captured_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
# PostgreSQL compatibility wrapper
# ---------------------------------------------------------------------------

_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Convert SQLite-flavoured SQL to PostgreSQL.

    Order matters:
      1. Rewrite `INSERT OR IGNORE` to `INSERT ... ON CONFLICT DO NOTHING`.
      2. Escape literal `%` (e.g. in `LIKE 'Player #%'`) to `%%` so psycopg2's
         format-substitution does NOT try to interpret it as a placeholder.
      3. Replace `?` with `%s` (psycopg2 parameter style).

    Step 2 must happen BEFORE step 3, otherwise the new `%s` placeholders we
    just introduced would get doubled into `%%s` and break parameter binding.
    """
    if _INSERT_OR_IGNORE_RE.search(sql):
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = sql.replace("%", "%%")
    sql = sql.replace("?", "%s")
    return sql


class _RowProxy:
    """Mimics sqlite3.Row: supports both row[i] and row['column_name']."""
    __slots__ = ("_values", "_colnames", "_dict")

    def __init__(self, values, colnames):
        self._values = tuple(values)
        self._colnames = tuple(colnames)
        self._dict = dict(zip(colnames, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._dict[key]

    def __contains__(self, key):
        return key in self._dict

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def get(self, key, default=None):
        return self._dict.get(key, default)

    def keys(self):
        return self._colnames


class _PgCursor:
    """psycopg2 cursor wrapped to look like sqlite3.Cursor."""

    def __init__(self, cur):
        self._cur = cur
        self._colnames: Optional[list] = None

    def execute(self, sql, params=None):
        self._cur.execute(_translate_sql(sql), params or ())
        if self._cur.description:
            self._colnames = [d[0] for d in self._cur.description]
        else:
            self._colnames = None
        return self

    def fetchall(self):
        rows = self._cur.fetchall()
        if not self._colnames:
            return rows
        return [_RowProxy(r, self._colnames) for r in rows]

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if not self._colnames:
            return row
        return _RowProxy(row, self._colnames)

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        self._cur.close()


class _PgConnection:
    """psycopg2 connection wrapped to look like sqlite3.Connection."""

    def __init__(self, real):
        self._conn = real
        self.row_factory = None  # ignored; present for sqlite3 API compat

    def execute(self, sql, params=None):
        cur = _PgCursor(self._conn.cursor())
        cur.execute(sql, params)
        return cur

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Connection factory + schema setup
# ---------------------------------------------------------------------------

def get_connection(db_path: Optional[Path] = None):
    """Open a backend-appropriate connection.

    PostgreSQL: psycopg2 wrapped in `_PgConnection` for sqlite3 API compat.
    SQLite:     real sqlite3.Connection with Row factory + FK pragma ON.
    """
    if USE_POSTGRES and psycopg2 is not None:
        real = psycopg2.connect(DATABASE_URL)
        return _PgConnection(real)

    p = db_path or DB_PATH
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create all tables and indexes. Idempotent.
    Runs at import time from app.py so gunicorn picks it up on Railway."""
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        for stmt in _schema_statements(USE_POSTGRES):
            cur.execute(stmt)
        for stmt in INDEXES:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Player name resolution (in-process cache, both backends)
# ---------------------------------------------------------------------------

_player_name_cache: dict[int, Optional[str]] = {}


def get_player_name(conn, player_id: int) -> Optional[str]:
    """Resolve player_id -> name. Caches hits + misses for process lifetime."""
    if player_id in _player_name_cache:
        return _player_name_cache[player_id]
    row = conn.execute("SELECT name FROM players WHERE id = ?", (player_id,)).fetchone()
    name = row["name"] if row else None
    _player_name_cache[player_id] = name
    return name


def clear_player_cache() -> None:
    _player_name_cache.clear()


# ---------------------------------------------------------------------------
# Run-as-script
# ---------------------------------------------------------------------------

def _report(db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        if USE_POSTGRES:
            tables = conn.execute(
                "SELECT tablename AS name FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            ).fetchall()
            print(f"DB:      PostgreSQL  ({DATABASE_URL.split('@', 1)[-1][:40] if '@' in DATABASE_URL else 'env'})")
        else:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            print(f"DB:      {db_path or DB_PATH}")

        print(f"backend: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
        print(f"tables:  {len(tables)}")
        for t in tables:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {t['name']}").fetchone()[0]
            print(f"  {t['name']:20s} {cnt:>7d}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    _report()
