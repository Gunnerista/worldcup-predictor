"""
app.py — Flask web interface for worldcup-predictor.

Routes:
  GET  /                     → today's match list (with model preview %)
  GET  /match/<id>           → PRE-MATCH consulting report
  GET  /stream/<id>          → Server-Sent Events (60s polling)
  GET  /live/<id>            → LIVE view (redirects to /match/<id> for now)
  GET  /post/<id>            → POST-MATCH view (redirects)
  POST /notes/<id>           → save user notes

HARD CONTRACT: every number rendered is COMPUTED by a model.py engine.
No raw BALLDONTLIE field is sent to the template as a probability,
percentage, similarity, motivation, or impact score.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date
from typing import Optional

import traceback

from flask import (
    Flask, render_template, request, Response,
    redirect, url_for, abort,
)
from werkzeug.exceptions import HTTPException

import database
import data_pipeline
import polymarket
from model import (
    EloEngine, TacticalEngine, PlayerMatchupEngine,
    PatternMatcher, NarrativeEngine, _load_historical_from_db,
    build_2026_elo, DixonColesEngine, save_prediction, compute_brier_score,
    INITIAL_RATINGS_2026,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

app = Flask(__name__)

# Ensure the SQLite schema exists at import time (runs under gunicorn too,
# not just `python app.py`). Idempotent: CREATE TABLE IF NOT EXISTS.
database.init_db()


@app.errorhandler(Exception)
def _handle_unexpected(e):
    """Catch every unhandled exception, dump traceback to stderr (Railway logs
    pick this up), and optionally render it in the browser if DEBUG_TRACEBACK=1.

    HTTP exceptions (404, 405, etc.) are passed through unchanged.
    """
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    print(f"\n!!! UNHANDLED EXCEPTION in {request.method} {request.path}\n{tb}",
          file=sys.stderr, flush=True)
    if os.environ.get("DEBUG_TRACEBACK", "").strip() in ("1", "true", "yes"):
        # Show real error to the user — only enable temporarily for diagnosis.
        body = (
            f"<h1>Internal Server Error</h1>"
            f"<h2>{type(e).__name__}: {e}</h2>"
            f"<pre style='background:#161b22;color:#e6edf3;padding:16px;"
            f"border-radius:8px;overflow:auto'>{tb}</pre>"
        )
        return body, 500
    return ("Internal Server Error", 500)

# --------------------------------------------------------------------------
# Engine state (warmed on first request)
# --------------------------------------------------------------------------

_elo: Optional[EloEngine] = None
_pattern_matcher: Optional[PatternMatcher] = None


# 2026 team-strength baselines now live in model.py as INITIAL_RATINGS_2026,
# derived from FIFA Men's World Ranking points (single source of truth, imported
# above). They seed every team so replaying 2018/2022 reflects both historical
# form AND broad strength (debutants aren't stuck at the 1500 default).


def _stage_key(stage_str: Optional[str]) -> str:
    s = (stage_str or "").lower()
    if "group" in s:        return "group"
    if "round of 16" in s:  return "round_of_16"
    if "quarter" in s:      return "quarter"
    if "semi" in s:         return "semi"
    if "final" in s:        return "final"
    return "group"


def _build_elo_from_history() -> EloEngine:
    """Seed every team with its 2026 strength baseline, then replay 2018+2022
    completed matches to nudge ratings toward recent form. Teams not in the
    baseline dict fall back to the engine's DEFAULT_ELO (1500)."""
    elo = EloEngine()

    # 1. Seed from baseline (covers every 2026 qualifier + most historicals)
    for team, baseline in INITIAL_RATINGS_2026.items():
        elo.set_rating(team, baseline)

    # 2. Replay 2018+2022 results in chronological order
    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT m.id, ht.name AS home, at.name AS away,
                   m.home_score, m.away_score, m.stage
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.status = 'completed' AND m.season IN (2018, 2022)
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY m.kickoff_utc ASC
        """).fetchall()
    finally:
        conn.close()

    for r in rows:
        hs, as_ = r["home_score"], r["away_score"]
        result = "draw" if hs == as_ else ("home_win" if hs > as_ else "away_win")
        elo.update(r["home"], r["away"], result, _stage_key(r["stage"]))
    return elo


def _build_live_elo_2026() -> EloEngine:
    """Prediction ELO source: live 2026 ratings (replayed from completed 2026
    results) layered over the INITIAL_RATINGS_2026 baseline.

    A team that has not yet played a 2026 match keeps its INITIAL_RATINGS_2026
    baseline; a team that has played uses its live 2026-derived rating from
    model.build_2026_elo().
    """
    elo = EloEngine()

    # 1. Baseline fallback for every 2026 qualifier.
    for team, baseline in INITIAL_RATINGS_2026.items():
        elo.set_rating(team, baseline)

    # 2. Overlay live 2026 ratings (FIFA-seeded top 10 + replayed results).
    for team, rating in build_2026_elo().items():
        elo.set_rating(team, rating)

    return elo


def _ensure_engines() -> None:
    global _elo, _pattern_matcher
    if _elo is None:
        print("[startup] building live 2026 ELO (results replay + baseline fallback)...")
        _elo = _build_live_elo_2026()
        print(f"[startup] ELO ready ({len(_elo.ratings)} teams)")
    if _pattern_matcher is None:
        print("[startup] loading PatternMatcher historical dataset...")
        _pattern_matcher = PatternMatcher(_load_historical_from_db())
        print(f"[startup] PatternMatcher ready ({len(_pattern_matcher.historical)} matches)")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _recent_team_stats(conn, team_name: str) -> dict:
    """Aggregated mean of last-3 completed matches for a team.

    The aggregation must run OVER the recent-3 subset, so ORDER BY + LIMIT
    happens inside a subquery; the outer SELECT then aggregates. Doing it in
    one statement (the previous shape) silently returned the average over
    ALL matches on SQLite, and failed strict-mode validation on PostgreSQL.
    """
    row = conn.execute("""
        SELECT
            AVG(possession)       AS possession_pct,
            AVG(xg_total)         AS xg_total,
            AVG(shots_total)      AS shots_total,
            AVG(shots_on_target)  AS shots_on_target,
            COUNT(*)              AS games
        FROM (
            SELECT
                ts.possession,
                ts.xg_total,
                ts.shots_total,
                ts.shots_on_target
            FROM team_stats ts
            JOIN teams t ON t.id = ts.team_id
            JOIN matches m ON m.id = ts.match_id
            WHERE t.name = ? AND m.status = 'completed'
            ORDER BY m.kickoff_utc DESC
            LIMIT 3
        ) recent
    """, (team_name,)).fetchone()

    if not row or not row["games"]:
        return {
            "possession_pct":  50.0,
            "xg_total":        0.0,
            "shots_total":     0.0,
            "shots_on_target": 0.0,
        }

    return {
        "possession_pct":  float(row["possession_pct"]    or 50),
        "xg_total":        float(row["xg_total"]          or 0),
        "shots_total":     float(row["shots_total"]       or 0),
        "shots_on_target": float(row["shots_on_target"]   or 0),
    }


def _zone_proxy(recent: dict) -> dict:
    """Synthesize a plausible zone-threat distribution from the recent average.
    Without per-shot location aggregated per zone, we distribute shots evenly
    and place 1 goal in the busiest zone. This is a deterministic placeholder
    until the data_pipeline computes per-zone aggregation.
    """
    shots = int(recent.get("shots_total", 0) or 0)
    on_target = int(recent.get("shots_on_target", 0) or 0)
    return {
        "shots_against_by_zone": {"left": shots // 3, "center": shots - 2 * (shots // 3), "right": shots // 3},
        "goals_against_by_zone": {"left": 0, "center": 1 if on_target > 4 else 0, "right": 0},
        "possession_pct": recent.get("possession_pct", 50.0),
    }


def _key_players_for(home_name: str, away_name: str) -> list:
    """Top 3 players across the two teams (by historical impact_score)."""
    engine = PlayerMatchupEngine()
    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT
                p.id AS player_id, p.name,
                AVG(ps.expected_goals)    AS expected_goals,
                AVG(ps.expected_assists)  AS expected_assists,
                AVG(ps.passes_accurate)   AS passes_accurate,
                AVG(ps.rating)            AS rating,
                COUNT(*)                  AS games
            FROM player_stats ps
            JOIN players p ON p.id = ps.player_id
            JOIN teams t ON t.id = ps.team_id
            WHERE t.name IN (?, ?)
              AND p.name NOT LIKE 'Player #%'
            GROUP BY p.id, p.name
            ORDER BY AVG(ps.expected_goals) DESC
            LIMIT 25
        """, (home_name, away_name)).fetchall()
    finally:
        conn.close()

    # Reconstruct a `passes_total` proxy assuming 85% pass accuracy (WC-level baseline).
    # This is necessary because the schema only stores `passes_accurate`, but the engine's
    # impact formula uses pass_rate = accurate / total. With this proxy, pass_rate ~= 0.85
    # uniformly, so the differentiator becomes xG / xA / volume, which is what we want.
    PASS_ACCURACY_PROXY = 0.85
    candidates = []
    for r in rows:
        passes_acc = float(r["passes_accurate"] or 0)
        candidates.append({
            "player_id":        r["player_id"],
            "name":             r["name"],
            "expected_goals":   float(r["expected_goals"]    or 0),
            "expected_assists": float(r["expected_assists"]  or 0),
            "passes_accurate":  passes_acc,
            "passes_total":     passes_acc / PASS_ACCURACY_PROXY if passes_acc > 0 else 0,
            "ball_recoveries":  0,
            "key_passes":       float(r["expected_assists"]  or 0),  # use xA as a key-pass proxy
        })
    return engine.get_key_players(team_id=None, match_stats=candidates, top_n=3)


def _load_user_notes(conn, match_id):
    """Latest user_notes row -> a notes_dict for DixonColesEngine.apply_user_notes.

    user_notes is free-text and per-match (no home/away split), so we map the
    'tactics' text to tactical_note (the '수비'/'공격' keyword detector reads it)
    and infer condition from 'player_condition' keywords. The note is attributed
    to the HOME side (the team the report is centred on); away_notes stays None.
    key_player_out can't be parsed from free text, so it is omitted.
    Returns (home_notes, away_notes).
    """
    try:
        row = conn.execute(
            "SELECT tactics, player_condition FROM user_notes "
            "WHERE match_id = ? ORDER BY created_at DESC",
            (match_id,),
        ).fetchone()
    except Exception:
        return None, None
    if not row:
        return None, None

    tactics = (row["tactics"] or "").strip()
    cond_text = (row["player_condition"] or "")
    notes: dict = {}
    if tactics:
        notes["tactical_note"] = tactics
    neg = any(k in cond_text for k in ("부상", "피로", "결장", "악화", "negative"))
    pos = any(k in cond_text for k in ("호조", "최상", "정상", "positive"))
    if neg and not pos:
        notes["condition"] = "negative"
    elif pos and not neg:
        notes["condition"] = "positive"

    return (notes or None), None


def _build_pre_match_bundle(match_id, conn=None) -> Optional[dict]:
    """Run every engine for a match and return a render-ready bundle.

    Win/draw/loss now comes from DixonColesEngine (group situation, player xG and
    user notes folded into λ); ELO is kept only for the displayed rating numbers
    and the narrator's reason text. Returns None if the match is unknown.
    """
    _ensure_engines()

    own_conn = conn is None
    if own_conn:
        conn = database.get_connection()
    else:
        import sqlite3 as _sq
        if isinstance(conn, _sq.Connection) and conn.row_factory is not _sq.Row:
            conn.row_factory = _sq.Row

    try:
        match = _load_match_from_db_or_api(match_id)
        if not match:
            return None

        tactical = TacticalEngine()
        player_engine = PlayerMatchupEngine()
        narrator = NarrativeEngine()

        home_name = (match.get("home_team") or {}).get("name", "Home")
        away_name = (match.get("away_team") or {}).get("name", "Away")
        home_cc   = (match.get("home_team") or {}).get("country_code")
        away_cc   = (match.get("away_team") or {}).get("country_code")
        group_name = (match.get("group") or {}).get("name")

        home_elo = _elo.get_rating(home_name)
        away_elo = _elo.get_rating(away_name)

        # ELO with 2026 host/proximity bonuses — kept for rating display + narrator.
        elo_result = _elo.get_win_probability_with_context(
            home_elo, away_elo,
            home_country=home_cc, away_country=away_cc, host_country="USA",
        )

        # Dixon-Coles W/D/L (group situation + player xG + user notes folded in).
        home_notes, away_notes = _load_user_notes(conn, match_id)
        dc = DixonColesEngine()
        pred = dc.predict_from_db(
            home_name, away_name,
            home_elo=home_elo, away_elo=away_elo,
            group_name=group_name,
            home_notes=home_notes, away_notes=away_notes,
            top_n=10,
        )
        wdl = pred["win_draw_loss"]

        home_recent = _recent_team_stats(conn, home_name)
        away_recent = _recent_team_stats(conn, away_name)

        home_prev = tactical.analyze_previous_match(home_name, _zone_proxy(home_recent))
        away_prev = tactical.analyze_previous_match(away_name, _zone_proxy(away_recent))

        poss_impact = tactical.calculate_possession_impact(
            home_recent["possession_pct"], away_recent["possession_pct"],
        )

        key_players = _key_players_for(home_name, away_name)

        xg_diff = round(home_recent["xg_total"] - away_recent["xg_total"], 2)

        current_features = {
            "xg_diff":              xg_diff,
            "elo_diff":             home_elo - away_elo,
            "possession_diff":      home_recent["possession_pct"] - away_recent["possession_pct"],
            "shots_diff":           home_recent["shots_total"]    - away_recent["shots_total"],
            "fatigue_diff":         0,
            "elimination_pressure": 0,
        }
        similar_matches = _pattern_matcher.find_similar_matches(current_features, top_n=3)
        upset_pct = _pattern_matcher.calculate_upset_probability(similar_matches)

        motiv_home = player_engine.world_cup_motivation_bonus(
            {"name": home_name},
            {"must_win": False, "already_qualified": False, "elo_diff": home_elo - away_elo},
        )
        motiv_away = player_engine.world_cup_motivation_bonus(
            {"name": away_name},
            {"must_win": False, "already_qualified": False, "elo_diff": away_elo - home_elo},
        )

        reasons = narrator.generate_reasons(
            elo_result, poss_impact, {"xg_diff": xg_diff},
        )
        key_lines = narrator.generate_key_matchups(key_players)
        warnings_list = narrator.generate_warning(similar_matches, poss_impact, upset_pct)
        warnings_list.append(f"⚠️ 역전 가능성 {upset_pct:.0f}%")

        # Polymarket: best-effort, never block the page
        poly_qual = poly_group = None
        try:
            poly_qual = polymarket.get_qualification_odds(home_name, round="16")
        except Exception as e:
            print(f"polymarket qual error: {e}")
        try:
            poly_group = polymarket.get_group_winner_odds(home_name)
        except Exception as e:
            print(f"polymarket group error: {e}")

        return {
            "match":         match,
            "home_name":     home_name,
            "away_name":     away_name,
            # W/D/L now from Dixon-Coles (keep home_pct/away_pct for the template).
            "home_pct":      round(wdl["home_win"]),
            "draw_pct":      round(wdl["draw"]),
            "away_pct":      round(wdl["away_win"]),
            "home_win_pct":  round(wdl["home_win"]),
            "away_win_pct":  round(wdl["away_win"]),
            "expected_goals": pred["expected_goals"],
            "scoreline_matrix": pred["top_scorelines"],   # top 10
            "situation":     pred.get("situation"),
            "model_used":    "Dixon-Coles v1",
            "elo_diff":      round(home_elo - away_elo),
            "home_elo":      round(home_elo),
            "away_elo":      round(away_elo),
            "home_prev":     home_prev,
            "away_prev":     away_prev,
            "poss_impact":   poss_impact,
            "key_players":   key_players,
            "key_lines":     key_lines,
            "similar_matches": similar_matches,
            "upset_pct":     upset_pct,
            "motiv_home":    motiv_home,
            "motiv_away":    motiv_away,
            "reasons":       reasons,
            "warnings":      warnings_list,
            "xg_diff":       xg_diff,
            "home_recent":   home_recent,
            "away_recent":   away_recent,
            "poly_qual_pct": round(poly_qual * 100) if poly_qual is not None else None,
            "poly_group_pct": round(poly_group * 100) if poly_group is not None else None,
        }
    finally:
        if own_conn:
            conn.close()


def _load_match_from_db_or_api(match_id: int) -> Optional[dict]:
    conn = database.get_connection()
    try:
        r = conn.execute("""
            SELECT m.*, ht.name AS home_name, ht.country_code AS home_cc, ht.id AS home_id,
                   at.name AS away_name, at.country_code AS away_cc, at.id AS away_id
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.id = ?
        """, (match_id,)).fetchone()
    finally:
        conn.close()

    if r:
        return {
            "id":         r["id"],
            "home_team":  {"id": r["home_id"], "name": r["home_name"], "country_code": r["home_cc"]},
            "away_team":  {"id": r["away_id"], "name": r["away_name"], "country_code": r["away_cc"]},
            "datetime":   r["kickoff_utc"],
            "home_score": r["home_score"],
            "away_score": r["away_score"],
            "status":     r["status"],
            "stage":      {"name": r["stage"]},
            "group":      {"name": r["group_name"]},
        }
    # Fallback: API search
    try:
        all_matches = data_pipeline.fetch_matches([2026])
    except Exception:
        return None
    return next((m for m in all_matches if m.get("id") == match_id), None)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    _ensure_engines()
    try:
        matches = data_pipeline.get_today_matches()
    except Exception as e:
        print(f"index: today fetch error: {e}")
        matches = []

    dc = DixonColesEngine()
    enriched = []
    for m in matches:
        home = (m.get("home_team") or {}).get("name", "?")
        away = (m.get("away_team") or {}).get("name", "?")
        group = (m.get("group") or {}).get("name", "")
        try:
            pred = dc.predict_from_db(
                home, away,
                home_elo=_elo.get_rating(home),
                away_elo=_elo.get_rating(away),
                group_name=group or None,
            )
            wdl = pred["win_draw_loss"]
            home_pct = round(wdl["home_win"])
            draw_pct = round(wdl["draw"])
            away_pct = round(wdl["away_win"])
            home_sit = (pred.get("situation") or {}).get("home") or {}
            note = home_sit.get("note")
        except Exception as e:
            print(f"index: predict error for {home} vs {away}: {e}")
            probs = _elo.get_win_probability(_elo.get_rating(home), _elo.get_rating(away))
            home_pct = round(probs["home"] * 100)
            draw_pct = round(probs["draw"] * 100)
            away_pct = round(probs["away"] * 100)
            note = None
        enriched.append({
            "id":         m.get("id"),
            "home_name":  home,
            "away_name":  away,
            "home_pct":   home_pct,
            "draw_pct":   draw_pct,
            "away_pct":   away_pct,
            "note":       note,
            "status":     m.get("status"),
            "kickoff":    m.get("datetime"),
            "home_score": m.get("home_score"),
            "away_score": m.get("away_score"),
            "group":      group,
        })

    return render_template("index.html", matches=enriched, today=date.today().isoformat())


@app.route("/api/brier")
def api_brier():
    """Model calibration: Brier score over completed, predicted 2026 matches."""
    res = compute_brier_score(season=2026)
    return {"brier_score": res["brier_score"], "n_matches": res["n_matches"]}


@app.route("/match/<int:match_id>")
def match_detail(match_id: int):
    bundle = _build_pre_match_bundle(match_id)
    if not bundle:
        abort(404)
    match = bundle["match"]

    # Phase detection: pre / live / post
    status = (match.get("status") or "").lower()
    if status in {"live", "in_progress"}:
        phase = "live"
    elif status in {"completed", "ft", "finished"}:
        phase = "post"
    else:
        phase = "pre"

    return render_template("match.html", bundle=bundle, phase=phase, match_id=match_id)


@app.route("/stream/<int:match_id>")
def stream(match_id: int):
    """SSE: emit current match snapshot every 60 seconds.

    Real event-driven recompute (goal/card -> immediate push) would require a
    background ingest worker. For now we poll the DB on each tick.
    """
    def generate():
        for _ in range(60):  # ~1 hour max stream lifetime
            try:
                conn = database.get_connection()
                row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
                conn.close()
                if row is not None:
                    payload = {
                        "home_score":  row["home_score"],
                        "away_score":  row["away_score"],
                        "status":      row["status"],
                        "ts":          time.time(),
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                else:
                    yield "data: {}\n\n"
            except GeneratorExit:
                break
            except Exception as e:
                yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
            time.sleep(60)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/live/<int:match_id>")
def live(match_id: int):
    return redirect(url_for("match_detail", match_id=match_id))


@app.route("/post/<int:match_id>")
def post_match(match_id: int):
    return redirect(url_for("match_detail", match_id=match_id))


@app.route("/notes/<int:match_id>", methods=["POST"])
def save_notes(match_id: int):
    tactics          = (request.form.get("tactics") or "").strip()
    player_condition = (request.form.get("player_condition") or "").strip()
    psychology       = (request.form.get("psychology") or "").strip()
    other            = (request.form.get("other") or "").strip()

    conn = database.get_connection()
    try:
        conn.execute("""
            INSERT INTO user_notes (match_id, tactics, player_condition, psychology, other)
            VALUES (?, ?, ?, ?, ?)
        """, (match_id, tactics, player_condition, psychology, other))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("match_detail", match_id=match_id))


# --------------------------------------------------------------------------
# Background live-sync worker
# --------------------------------------------------------------------------
#
# Polls BALLDONTLIE via data_pipeline.sync_live_lite() so the DB (and thus the
# SSE stream) actually receives fresh 2026 scores. Cadence adapts to whether a
# match is live: 60s with live matches, 300s otherwise.
#
# RATE-LIMIT CAVEAT: with multiple gunicorn workers, EACH worker process starts
# its own thread -> the API is hit Nx. On Railway run a single web worker, or
# set ENABLE_LIVE_SYNC=0 on all but one. The Lock below only guards against
# overlap WITHIN a process, not across worker processes.

_sync_lock = threading.Lock()
_sync_thread_started = False

LIVE_INTERVAL_SEC = 60
IDLE_INTERVAL_SEC = 300
PREDICT_WINDOW_HOURS = 2   # save a prediction within this window before kickoff


def _run_due_predictions() -> int:
    """Save a pre-kickoff prediction for any 2026 match kicking off within
    PREDICT_WINDOW_HOURS, that doesn't already have one. Returns count saved."""
    from datetime import datetime, timezone, timedelta

    _ensure_engines()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=PREDICT_WINDOW_HOURS)

    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT m.id AS id, ht.name AS home, at.name AS away,
                   m.kickoff_utc AS k, m.group_name AS grp
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = 2026 AND m.status = 'scheduled'
              AND m.id NOT IN (SELECT match_id FROM predictions)
        """).fetchall()

        dc = DixonColesEngine()
        saved = 0
        for r in rows:
            k = r["k"]
            if not k:
                continue
            try:
                ko = datetime.fromisoformat(str(k).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ko.tzinfo is None:
                ko = ko.replace(tzinfo=timezone.utc)
            if not (now <= ko <= horizon):
                continue

            home, away = r["home"], r["away"]
            pred = dc.predict_from_db(
                home, away,
                home_elo=_elo.get_rating(home),
                away_elo=_elo.get_rating(away),
                group_name=r["grp"],
            )
            wdl = pred["win_draw_loss"]
            save_prediction(r["id"], wdl["home_win"], wdl["draw"], wdl["away_win"],
                            model_version="v1", conn=conn)
            saved += 1

        if saved:
            conn.commit()
        return saved
    finally:
        conn.close()


def _live_sync_loop() -> None:
    global _elo
    while True:
        interval = IDLE_INTERVAL_SEC
        try:
            with _sync_lock:
                result = data_pipeline.sync_live_lite()
            print(f"[sync] updated {result['updated']} matches "
                  f"(seen={result['matches_seen']}, "
                  f"completed+={result['newly_completed']}, "
                  f"live={result['in_progress']})", flush=True)

            # A match just finished -> pull its player-level data once.
            if result.get("newly_completed", 0) > 0:
                try:
                    with _sync_lock:
                        enriched = data_pipeline.enrich_completed_matches_2026()
                    print(f"[sync] enriched {enriched['enriched']} matches "
                          f"(player_stats={enriched['player_stats']}, "
                          f"shots={enriched['shots']}, "
                          f"events={enriched['events']})", flush=True)
                except Exception as e:
                    # Enrich failure must not kill the sync loop.
                    print(f"[sync] enrich error: {e}", file=sys.stderr, flush=True)

                # New results changed the standings -> drop the cached engine so
                # the next prediction request rebuilds ELO (same lazy pattern as
                # _ensure_engines). GIL makes this assignment atomic.
                _elo = None
                print("[sync] ELO invalidated; rebuilds on next prediction", flush=True)

            # Save pre-kickoff predictions for matches starting soon.
            try:
                n_pred = _run_due_predictions()
                if n_pred:
                    print(f"[sync] saved {n_pred} pre-kickoff predictions", flush=True)
            except Exception as e:
                print(f"[sync] prediction error: {e}", file=sys.stderr, flush=True)

            interval = LIVE_INTERVAL_SEC if result.get("in_progress", 0) > 0 else IDLE_INTERVAL_SEC
        except Exception as e:
            # Never let the worker thread die — log and keep looping.
            print(f"[sync] error: {e}", file=sys.stderr, flush=True)
            interval = IDLE_INTERVAL_SEC
        time.sleep(interval)


def _start_sync_thread() -> None:
    global _sync_thread_started
    if _sync_thread_started:
        return
    if os.environ.get("ENABLE_LIVE_SYNC", "1").strip() not in ("1", "true", "yes"):
        print("[sync] live-sync disabled (ENABLE_LIVE_SYNC)", flush=True)
        return
    _sync_thread_started = True
    threading.Thread(target=_live_sync_loop, name="live-sync", daemon=True).start()
    print("[sync] background live-sync thread started", flush=True)


# Start at import time so gunicorn (not just `python app.py`) runs the worker.
_start_sync_thread()


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
