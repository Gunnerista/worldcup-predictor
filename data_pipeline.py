"""
data_pipeline.py
================

BALLDONTLIE FIFA Worldcup ingestion + sync.

Project rules enforced here (imported from `database.py`):
  * Filter parameter MUST be `match_ids[]=...` (array form).
    The single-key form `match_id=` is silently ignored by the API.
  * Every API call sleeps `API_THROTTLE_SECONDS` before sending. GOAT plan
    is 600/min but short bursts hit 429.
  * Player names are resolved through `database.get_player_name()` after a
    bulk backfill, never row-by-row over HTTP.

CLI:
    python data_pipeline.py backfill      # 2018 + 2022 full backfill
    python data_pipeline.py sync          # 2026 sync (matches + per-match data)
    python data_pipeline.py today         # today's 2026 matches
    python data_pipeline.py live          # currently-in-progress matches
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime
from typing import Any, Iterable

import requests
from dotenv import load_dotenv

import database

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

load_dotenv()

BASE        = database.BALLDONTLIE_BASE
THROTTLE    = database.API_THROTTLE_SECONDS
MATCH_KEY   = database.MATCH_FILTER_KEY   # "match_ids[]"  (enforced everywhere)

API_KEY     = os.getenv("BALLDONTLIE_API_KEY", "").strip()

BATCH_SIZE_MATCHES = 10
PAGE_SIZE          = 100
TIMEOUT_SEC        = 30

_session = requests.Session()
_session.headers["Authorization"] = API_KEY


# --------------------------------------------------------------------------
# Low-level HTTP
# --------------------------------------------------------------------------

_MAX_429_RETRIES = 8


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """Throttled GET that respects BALLDONTLIE's rate-limit response headers.

    Strategy:
      - Sleep THROTTLE before every call (baseline pacing).
      - On 200: read `x-ratelimit-remaining`; if it's 0 or 1, proactively sleep
        until `x-ratelimit-reset` so the NEXT call doesn't 429.
      - On 429: sleep until `x-ratelimit-reset` (or exponential fallback).

    This is necessary because the active API key reports `x-ratelimit-limit: 5`
    (not the 600/min GOAT cap). Static throttling can't keep up; dynamic
    pacing on the headers is the only reliable approach.
    """
    url = f"{BASE}{path}"
    backoff = 1.0
    last_resp = None
    for attempt in range(_MAX_429_RETRIES):
        time.sleep(THROTTLE)
        r = _session.get(url, params=params, timeout=TIMEOUT_SEC)
        last_resp = r

        remaining_hdr = r.headers.get("x-ratelimit-remaining")
        reset_hdr     = r.headers.get("x-ratelimit-reset")

        if r.status_code != 429:
            r.raise_for_status()
            # Proactive: if we used up the bucket, sleep until reset
            try:
                rem  = int(remaining_hdr) if remaining_hdr is not None else None
                rst  = int(reset_hdr)     if reset_hdr     is not None else None
            except ValueError:
                rem, rst = None, None
            if rem is not None and rst is not None and rem <= 1:
                wait = max(0.0, float(rst) - time.time() + 1.0)
                if 0 < wait < 90:
                    time.sleep(wait)
            try:
                return r.json()
            except ValueError:
                return {}

        # 429 path: sleep until reset (header) or exponential fallback
        wait = backoff
        try:
            if reset_hdr is not None:
                wait = max(backoff, float(int(reset_hdr)) - time.time() + 1.0)
        except ValueError:
            pass
        time.sleep(min(max(wait, 1.0), 90.0))
        backoff = min(backoff * 2, 64.0)

    if last_resp is not None:
        last_resp.raise_for_status()
    return {}


def _paginate(path: str, params: dict[str, Any] | None = None,
              progress_label: str | None = None) -> list[dict]:
    """Walk all cursor pages and return the flattened data list."""
    out: list[dict] = []
    cursor = None
    page = 0
    while True:
        p = dict(params or {})
        p["per_page"] = PAGE_SIZE
        if cursor:
            p["cursor"] = cursor
        payload = _get(path, p)
        data = payload.get("data") or []
        out.extend(data)
        page += 1
        if progress_label and page % 5 == 0:
            print(f"      {progress_label}: page {page}, accumulated {len(out)}")
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _fetch_for_matches(path: str, match_ids: list[int]) -> list[dict]:
    """Batched + paginated fetch keyed on match_ids[]=.... Enforces MATCH_KEY."""
    if not match_ids:
        return []
    out: list[dict] = []
    for i in range(0, len(match_ids), BATCH_SIZE_MATCHES):
        chunk = match_ids[i:i + BATCH_SIZE_MATCHES]
        base_params = {MATCH_KEY: [str(m) for m in chunk]}
        cursor = None
        while True:
            p = dict(base_params)
            p["per_page"] = PAGE_SIZE
            if cursor:
                p["cursor"] = cursor
            payload = _get(path, p)
            data = payload.get("data") or []
            out.extend(data)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
    return out


# --------------------------------------------------------------------------
# Endpoint fetchers
# --------------------------------------------------------------------------

def fetch_teams() -> list[dict]:
    return _paginate("/teams", progress_label="teams")


def fetch_players() -> list[dict]:
    return _paginate("/players", progress_label="players")


def fetch_matches(seasons: Iterable[int]) -> list[dict]:
    params = {"seasons[]": [str(s) for s in seasons]}
    return _paginate("/matches", params=params, progress_label="matches")


def fetch_rosters_by_team(team_id: int, seasons: Iterable[int] | None = None) -> list[dict]:
    """Per-team roster. Used to resolve player names after a placeholder backfill."""
    params: dict[str, Any] = {"team_ids[]": [str(team_id)]}
    if seasons:
        params["seasons[]"] = [str(s) for s in seasons]
    return _paginate("/rosters", params=params)


def resolve_player_names(conn, seasons: Iterable[int] = (2018, 2022, 2026)) -> int:
    """Walk every team in DB, fetch its roster(s), UPDATE players.name.
    Returns number of rows updated. Idempotent and slow (one HTTP call per team)."""
    rows = conn.execute("SELECT id FROM teams").fetchall()
    team_ids = [r["id"] for r in rows]
    updated = 0
    for i, tid in enumerate(team_ids, 1):
        try:
            roster = fetch_rosters_by_team(tid, seasons=seasons)
        except Exception as e:
            print(f"      roster team_id={tid}: error {e}")
            continue
        for entry in roster:
            player = entry.get("player") or entry
            pid = player.get("id")
            pname = player.get("name")
            if not pid or not pname:
                continue
            res = conn.execute(
                "UPDATE players SET name = ?, team_id = COALESCE(team_id, ?) "
                "WHERE id = ? AND (name IS NULL OR name LIKE 'Player #%')",
                (pname, tid, pid),
            )
            updated += res.rowcount
        if i % 10 == 0:
            print(f"      rosters: team {i}/{len(team_ids)}  names updated so far: {updated}")
    conn.commit()
    return updated


# --------------------------------------------------------------------------
# Upserts (idempotent via INSERT OR IGNORE)
# --------------------------------------------------------------------------

def upsert_teams(conn, teams: list[dict]) -> None:
    cur = conn.cursor()
    for t in teams:
        cur.execute(
            "INSERT OR IGNORE INTO teams (id, name, abbreviation, country_code) "
            "VALUES (?, ?, ?, ?)",
            (t.get("id"), t.get("name"), t.get("abbreviation"), t.get("country_code")),
        )
    conn.commit()


def upsert_players(conn, players: list[dict]) -> None:
    """Resolve team_id via country_code -> teams lookup (national team)."""
    country_to_team: dict[str, int] = {}
    for row in conn.execute("SELECT id, country_code FROM teams").fetchall():
        cc = row["country_code"]
        if cc:
            country_to_team[cc] = row["id"]

    cur = conn.cursor()
    for p in players:
        tid = country_to_team.get(p.get("country_code"))
        cur.execute(
            "INSERT OR IGNORE INTO players (id, name, team_id) VALUES (?, ?, ?)",
            (p.get("id"), p.get("name"), tid),
        )
    conn.commit()


def _ensure_player(conn, player_id, team_id=None) -> None:
    """Insert a stub player row (placeholder name) if the id is unknown.
    Real names get resolved later via fetch_rosters_by_team()."""
    if player_id is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO players (id, name, team_id) VALUES (?, ?, ?)",
        (player_id, f"Player #{player_id}", team_id),
    )


def _ensure_team(conn, team: dict | None) -> None:
    """Insert a team row from a nested match-payload team object if missing.

    /teams returns only the 2026 qualifier set. 2018/2022 had different
    participants; those team rows come from the match payload itself.
    """
    if not team or team.get("id") is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO teams (id, name, abbreviation, country_code) "
        "VALUES (?, ?, ?, ?)",
        (team.get("id"), team.get("name"), team.get("abbreviation"), team.get("country_code")),
    )


def upsert_matches(conn, matches: list[dict]) -> None:
    cur = conn.cursor()
    for m in matches:
        home_team = m.get("home_team") or {}
        away_team = m.get("away_team") or {}
        _ensure_team(conn, home_team)
        _ensure_team(conn, away_team)

        season = (m.get("season") or {}).get("year")
        stage  = (m.get("stage") or {}).get("name")
        group  = (m.get("group") or {}).get("name")
        cur.execute(
            """INSERT OR IGNORE INTO matches
               (id, season, stage, group_name, home_team_id, away_team_id,
                home_score, away_score, kickoff_utc, status,
                home_formation, away_formation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m.get("id"), season, stage, group, home_team.get("id"), away_team.get("id"),
             m.get("home_score"), m.get("away_score"),
             m.get("datetime"), m.get("status"),
             m.get("home_formation"), m.get("away_formation")),
        )
    conn.commit()


def upsert_team_stats(conn, items: list[dict]) -> None:
    cur = conn.cursor()
    for it in items:
        cur.execute(
            """INSERT OR IGNORE INTO team_stats
               (match_id, team_id, is_home, possession, shots_total, shots_on_target,
                xg_total, xgot_total, corners, fouls)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it.get("match_id"), it.get("team_id"),
             1 if it.get("is_home") else 0,
             it.get("possession_pct"),                       # API -> our column
             it.get("shots_total"), it.get("shots_on_target"),
             it.get("expected_goals"), None,                 # xgot_total derived later
             it.get("corners"), it.get("fouls")),
        )
    conn.commit()


def upsert_player_stats(conn, items: list[dict]) -> None:
    cur = conn.cursor()
    for it in items:
        _ensure_player(conn, it.get("player_id"), it.get("team_id"))
        cur.execute(
            """INSERT OR IGNORE INTO player_stats
               (match_id, player_id, team_id, minutes_played,
                expected_goals, expected_assists, shots, passes_accurate, rating)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it.get("match_id"), it.get("player_id"), it.get("team_id"),
             it.get("minutes_played"),
             it.get("expected_goals"), it.get("expected_assists"),
             None,                                            # shots derived later
             it.get("passes_accurate"),
             it.get("rating")),
        )
    conn.commit()


def upsert_shots(conn, items: list[dict]) -> None:
    cur = conn.cursor()
    for it in items:
        _ensure_player(conn, it.get("player_id"), it.get("team_id"))
        cur.execute(
            """INSERT OR IGNORE INTO match_shots
               (match_id, player_id, team_id, minute, xg, xgot,
                player_x, player_y, body_part, situation, shot_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it.get("match_id"), it.get("player_id"), it.get("team_id"),
             it.get("time_minute"),
             it.get("xg"), it.get("xgot"),
             it.get("player_x"), it.get("player_y"),
             it.get("body_part"), it.get("situation"), it.get("shot_type")),
        )
    conn.commit()


def upsert_momentum(conn, items: list[dict]) -> None:
    """Signed `value` -> home/away momentum (see database.py docstring)."""
    cur = conn.cursor()
    for it in items:
        v = it.get("value")
        if v is None:
            home_m, away_m = None, None
        else:
            v = float(v)
            home_m = v if v >= 0 else 0.0
            away_m = 0.0 if v >= 0 else -v
        cur.execute(
            """INSERT OR IGNORE INTO match_momentum
               (match_id, minute, home_momentum, away_momentum)
               VALUES (?, ?, ?, ?)""",
            (it.get("match_id"), it.get("minute"), home_m, away_m),
        )
    conn.commit()


def upsert_events(conn, items: list[dict]) -> None:
    """API quirks for /match_events:
      - event type key is `incident_type` (card/goal/...); `incident_class` is subtype
      - player is nested: {"player": {"id": ..., "name": ...}}
      - no team_id; only `is_home` -> resolved via matches table
      - player.name is available — opportunistically upgrade placeholder names
    """
    cur = conn.cursor()

    # Resolve home/away team_id for each match in this batch
    match_ids = {it.get("match_id") for it in items if it.get("match_id") is not None}
    team_lookup: dict[int, dict] = {}
    if match_ids:
        q = "SELECT id, home_team_id, away_team_id FROM matches WHERE id IN ({})".format(
            ",".join("?" * len(match_ids))
        )
        for r in conn.execute(q, list(match_ids)).fetchall():
            team_lookup[r["id"]] = {"home": r["home_team_id"], "away": r["away_team_id"]}

    for it in items:
        mid = it.get("match_id")
        is_home = it.get("is_home")
        team_id = None
        if mid in team_lookup and is_home is not None:
            team_id = team_lookup[mid]["home" if is_home else "away"]

        player_obj = it.get("player") or {}
        pid = player_obj.get("id")
        pname = player_obj.get("name")
        if pid:
            # Opportunistically upgrade placeholder name with real name from event payload
            _ensure_player(conn, pid, team_id)
            if pname:
                conn.execute(
                    "UPDATE players SET name = ?, team_id = COALESCE(team_id, ?) "
                    "WHERE id = ? AND name LIKE 'Player #%'",
                    (pname, team_id, pid),
                )

        event_type = it.get("incident_type") or it.get("incident_class") or it.get("event_type")
        if not event_type:
            continue   # skip malformed rows

        cur.execute(
            """INSERT INTO match_events
               (match_id, minute, event_type, team_id, player_id)
               VALUES (?, ?, ?, ?, ?)""",
            (mid, it.get("time_minute") or it.get("minute"), event_type, team_id, pid),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Derived columns: xgot_total (team) + shots (player) from match_shots
# --------------------------------------------------------------------------

def compute_derived(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        UPDATE team_stats
        SET xgot_total = COALESCE((
            SELECT SUM(xgot) FROM match_shots
            WHERE match_shots.match_id = team_stats.match_id
              AND match_shots.team_id  = team_stats.team_id
        ), 0)
    """)
    cur.execute("""
        UPDATE player_stats
        SET shots = COALESCE((
            SELECT COUNT(*) FROM match_shots
            WHERE match_shots.match_id  = player_stats.match_id
              AND match_shots.player_id = player_stats.player_id
        ), 0)
    """)
    conn.commit()


# --------------------------------------------------------------------------
# High-level operations
# --------------------------------------------------------------------------

_PER_MATCH_ENDPOINTS = [
    ("/team_match_stats",   upsert_team_stats,   "team_stats"),
    ("/player_match_stats", upsert_player_stats, "player_stats"),
    ("/match_shots",        upsert_shots,        "shots"),
    ("/match_momentum",     upsert_momentum,     "momentum"),
    ("/match_events",       upsert_events,       "events"),
]


def _ingest_per_match_data(conn, match_ids: list[int], label: str = "") -> None:
    total = len(match_ids)
    if total == 0:
        return
    for path, upsert_fn, name in _PER_MATCH_ENDPOINTS:
        print(f"      {name:14s} <- {path}")
        done = 0
        for i in range(0, total, BATCH_SIZE_MATCHES):
            chunk = match_ids[i:i + BATCH_SIZE_MATCHES]
            items: list[dict] = []
            base_params = {MATCH_KEY: [str(m) for m in chunk]}
            cursor = None
            while True:
                p = dict(base_params)
                p["per_page"] = PAGE_SIZE
                if cursor:
                    p["cursor"] = cursor
                payload = _get(path, p)
                items.extend(payload.get("data") or [])
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            try:
                upsert_fn(conn, items)
            except Exception as e:
                print(f"        WARN upsert failed for {name} batch {i}: {e}")
            done += len(chunk)
            print(f"        경기 {done}/{total} 수집 중... rows={len(items)}")


def backfill_historical(seasons: tuple[int, ...] = (2018, 2022)) -> None:
    """Idempotent full backfill of historical World Cups.
    Re-running is safe (INSERT OR IGNORE).

    NOTE: /players bulk pagination triggers BALLDONTLIE's burst limiter
    (the endpoint exposes ~30k+ player rows). We skip it here. Player rows
    are registered with placeholder names as their IDs appear in per-match
    data; use resolve_player_names() afterwards to fill real names via /rosters.
    """
    print(f"Backfill seasons: {list(seasons)}")
    database.init_db()
    conn = database.get_connection()
    try:
        print("[1/3] teams...")
        teams = fetch_teams()
        upsert_teams(conn, teams)
        print(f"      teams API={len(teams)}  DB={conn.execute('SELECT COUNT(*) FROM teams').fetchone()[0]}")

        print("[2/3] matches...")
        matches = fetch_matches(seasons)
        upsert_matches(conn, matches)
        match_ids = [m["id"] for m in matches if m.get("id") is not None]
        print(f"      matches API={len(matches)}  ids={len(match_ids)}")

        print("[3/3] per-match data (placeholder player names auto-registered)...")
        _ingest_per_match_data(conn, match_ids)

        print("Computing derived columns (team_stats.xgot_total, player_stats.shots)...")
        compute_derived(conn)

        print()
        print("=" * 60)
        print("BACKFILL COMPLETE")
        print("=" * 60)
        for table in ("teams", "players", "matches", "team_stats", "player_stats",
                      "match_shots", "match_momentum", "match_events"):
            cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:20s} {cnt:>7d}")
    finally:
        conn.close()


def sync_live() -> None:
    """Refresh 2026 season + ingest per-match data for completed/live matches."""
    print("Sync 2026 live...")
    database.init_db()
    conn = database.get_connection()
    try:
        matches = fetch_matches([2026])
        upsert_matches(conn, matches)
        match_ids = [
            m["id"] for m in matches
            if (m.get("status") or "").lower() in {"live", "in_progress", "completed", "ft"}
        ]
        if match_ids:
            _ingest_per_match_data(conn, match_ids)
            compute_derived(conn)
        print(f"sync done. 2026 matches: {len(matches)}, ingested: {len(match_ids)}")
    finally:
        conn.close()


def sync_live_lite() -> dict:
    """Rate-limit-safe 2026 live refresh.

    API budget per call (key limit is ~5 req/min):
      - /matches (2026)                     : 1 paginated fetch (always)
      - /team_match_stats                   : only for matches whose status
                                              flipped to 'completed' since the
                                              last sync (batched)
      - /match_momentum                     : only for in-progress/live matches
                                              (batched)

    Deliberately does NOT call player_match_stats / match_shots / match_events
    to protect the rate limit during a live matchday.

    Scores/status are written with an explicit UPDATE because upsert_matches()
    uses INSERT OR IGNORE, which never touches existing rows.

    Returns: {matches_seen, updated, newly_completed, in_progress}
    """
    database.init_db()
    conn = database.get_connection()
    try:
        matches = fetch_matches([2026])

        # Snapshot current DB state to detect score/status changes.
        prev: dict[int, dict] = {}
        for r in conn.execute(
            "SELECT id, home_score, away_score, status FROM matches WHERE season = ?",
            (2026,),
        ).fetchall():
            prev[r["id"]] = {
                "home_score": r["home_score"],
                "away_score": r["away_score"],
                "status": (r["status"] or "").lower(),
            }

        # Insert brand-new matches (also ensures team rows exist).
        upsert_matches(conn, matches)

        newly_completed: list[int] = []
        in_progress: list[int] = []
        updated = 0

        cur = conn.cursor()
        for m in matches:
            mid = m.get("id")
            if mid is None:
                continue
            status = (m.get("status") or "").lower()
            hs = m.get("home_score")
            as_ = m.get("away_score")

            before = prev.get(mid)
            # INSERT OR IGNORE won't update existing rows -> explicit UPDATE.
            cur.execute(
                "UPDATE matches SET home_score = ?, away_score = ?, status = ? "
                "WHERE id = ?",
                (hs, as_, m.get("status"), mid),
            )

            if before is None:
                updated += 1
            elif (before["home_score"] != hs
                  or before["away_score"] != as_
                  or before["status"] != status):
                updated += 1

            if status == "completed" and (before is None or before["status"] != "completed"):
                newly_completed.append(mid)
            if status in {"in_progress", "live"}:
                in_progress.append(mid)

        conn.commit()

        # Newly completed -> one team_match_stats pull (final box score).
        if newly_completed:
            items = _fetch_for_matches("/team_match_stats", newly_completed)
            upsert_team_stats(conn, items)

        # In-progress -> momentum only.
        if in_progress:
            items = _fetch_for_matches("/match_momentum", in_progress)
            upsert_momentum(conn, items)

        return {
            "matches_seen": len(matches),
            "updated": updated,
            "newly_completed": len(newly_completed),
            "in_progress": len(in_progress),
        }
    finally:
        conn.close()


def get_today_matches(today: date | None = None) -> list[dict]:
    """2026 matches scheduled for the given UTC date (defaults to today)."""
    target = today or date.today()
    matches = fetch_matches([2026])
    out: list[dict] = []
    for m in matches:
        ts = m.get("datetime")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.date() == target:
            out.append(m)
    return out


def get_live_matches() -> list[dict]:
    matches = fetch_matches([2026])
    return [
        m for m in matches
        if (m.get("status") or "").lower() in {"live", "in_progress"}
    ]


def get_match_detail(match_id: int) -> dict:
    """All per-match endpoint payloads for a single match."""
    out: dict[str, Any] = {"match_id": match_id}
    for path, _, label in _PER_MATCH_ENDPOINTS:
        payload = _get(path, {MATCH_KEY: [str(match_id)], "per_page": PAGE_SIZE})
        out[label] = payload.get("data") or []
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cli() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "backfill":
        backfill_historical(seasons=(2018, 2022))
        return 0

    if cmd == "events":
        # Re-run JUST the events endpoint over all matches in DB.
        conn = database.get_connection()
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM matches ORDER BY id").fetchall()]
            print(f"re-ingesting events for {len(ids)} matches...")
            _ingest_per_match_data(conn, ids[:0])  # no-op warmup
            # call only the events endpoint, reusing the per-match loop machinery
            from itertools import islice
            total = len(ids)
            done = 0
            for i in range(0, total, BATCH_SIZE_MATCHES):
                chunk = ids[i:i + BATCH_SIZE_MATCHES]
                items: list[dict] = []
                base_params = {MATCH_KEY: [str(m) for m in chunk]}
                cursor = None
                while True:
                    p = dict(base_params)
                    p["per_page"] = PAGE_SIZE
                    if cursor:
                        p["cursor"] = cursor
                    payload = _get("/match_events", p)
                    items.extend(payload.get("data") or [])
                    cursor = (payload.get("meta") or {}).get("next_cursor")
                    if not cursor:
                        break
                upsert_events(conn, items)
                done += len(chunk)
                print(f"  events: 경기 {done}/{total}  rows={len(items)}")
            print(f"events in DB now: {conn.execute('SELECT COUNT(*) FROM match_events').fetchone()[0]}")
        finally:
            conn.close()
        return 0

    if cmd == "names":
        conn = database.get_connection()
        try:
            n = resolve_player_names(conn, seasons=(2018, 2022, 2026))
            print(f"player names resolved: {n}")
        finally:
            conn.close()
        return 0

    if cmd == "sync":
        sync_live()
        return 0

    if cmd == "today":
        matches = get_today_matches()
        print(f"Today ({date.today()}): {len(matches)} matches")
        for m in matches:
            ht = (m.get("home_team") or {}).get("name", "?")
            at = (m.get("away_team") or {}).get("name", "?")
            print(f"  {m.get('datetime')}  {ht} vs {at}  ({m.get('status')})")
        return 0

    if cmd == "live":
        matches = get_live_matches()
        print(f"Live now: {len(matches)} matches")
        for m in matches:
            ht = (m.get("home_team") or {}).get("name", "?")
            at = (m.get("away_team") or {}).get("name", "?")
            print(f"  {ht} {m.get('home_score', '?')}-{m.get('away_score', '?')} {at}")
        return 0

    print("Usage: python data_pipeline.py {backfill|sync|today|live}")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not API_KEY or API_KEY == "your_key_here":
        print("FATAL: BALLDONTLIE_API_KEY missing/placeholder in .env")
        sys.exit(2)
    sys.exit(_cli())
