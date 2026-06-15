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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import traceback

from flask import (
    Flask, render_template, request, Response,
    redirect, url_for, abort,
)
from werkzeug.exceptions import HTTPException

import database
import data_pipeline
from model import (
    EloEngine, PatternMatcher, _load_historical_from_db,
    build_2026_elo, DixonColesEngine, save_prediction, compute_brier_score,
    INITIAL_RATINGS_2026, _team_strengths_from_db,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

app = Flask(__name__, static_folder="static", static_url_path="/static")

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
    banner = (f"\n=== 500 ERROR === {request.method} {request.path}\n"
              f"{type(e).__name__}: {e}\n{tb}")
    # Print to BOTH streams so it shows up regardless of how Railway tails logs.
    print(banner, file=sys.stderr, flush=True)
    print(banner, flush=True)
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


def _load_user_notes(conn, match_id):
    """Latest user_notes row -> a notes_dict for DixonColesEngine.apply_user_notes.

    user_notes is free-text and per-match (no team1/team2 split), so we map the
    'tactics' text to tactical_note (the 'defensive'/'attacking' keyword detector
    reads it) and infer condition from 'player_condition' keywords. The note is
    attributed to team1 (the side the report is centred on); the second team's
    notes stay None. key_player_out can't be parsed from free text, so it is
    omitted. Returns (team1_notes, team2_notes).
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
    cond_text = (row["player_condition"] or "").lower()
    notes: dict = {}
    if tactics:
        notes["tactical_note"] = tactics
    neg = any(k in cond_text for k in ("injury", "injured", "fatigue", "tired", "out", "doubt", "negative"))
    pos = any(k in cond_text for k in ("fit", "sharp", "fresh", "back", "positive"))
    if neg and not pos:
        notes["condition"] = "negative"
    elif pos and not neg:
        notes["condition"] = "positive"

    return (notes or None), None


def _team_code(name: str) -> str:
    """Short uppercase code for the math block (e.g. 'Belgium' -> 'BEL')."""
    letters = "".join(c for c in (name or "") if c.isalpha())
    return letters[:3].upper() or "TM"


def _match_strength(conn, team_name):
    """Average xG for / against for a team across all matches with xG data.
    Returns (xg_for, xg_against) — either may be None when no xG exists."""
    try:
        row = conn.execute(
            """
            SELECT AVG(ts.xg_total)  AS xf,
                   AVG(opp.xg_total) AS xa
            FROM team_stats ts
            JOIN team_stats opp ON opp.match_id = ts.match_id AND opp.team_id != ts.team_id
            JOIN teams t ON t.id = ts.team_id
            WHERE t.name = ?
            """,
            (team_name,),
        ).fetchone()
    except Exception:
        return None, None
    if not row:
        return None, None
    return row["xf"], row["xa"]


def _matchday(conn, group_name, kickoff):
    """Group matchday (1-3) from chronological order; None for knockout."""
    if not group_name or not kickoff:
        return None
    try:
        rows = conn.execute(
            "SELECT kickoff_utc FROM matches WHERE season = 2026 AND group_name = ? "
            "ORDER BY kickoff_utc",
            (group_name,),
        ).fetchall()
    except Exception:
        return None
    ks = [r["kickoff_utc"] for r in rows]
    if kickoff in ks:
        return ks.index(kickoff) // 2 + 1   # 4-team group: 2 matches / matchday
    return None


def generate_pre_match_narrative(b) -> str:
    """English pre-match analysis assembled from model outputs."""
    t1, t2 = b["team1"], b["team2"]
    elo_diff = b["elo_diff"]
    total_xg = b["eg1"] + b["eg2"]
    stronger, sp = (t1, b["p1"]) if b["p1"] >= b["p2"] else (t2, b["p2"])
    parts = []
    if abs(elo_diff) >= 1:
        parts.append(
            f"ELO gap of {abs(elo_diff)} points favours {stronger}, whom the model "
            f"gives a {sp}% win probability."
        )
    else:
        parts.append(
            f"The sides are level on ELO; the model sees a near-even contest "
            f"({b['p1']}% / {b['p2']}%)."
        )
    parts.append(f"Draw probability sits at {b['pdraw']}%.")
    pace = "low-scoring" if total_xg < 2.69 else "open, high-tempo"
    parts.append(
        f"Combined expected goals total {total_xg:.2f}, "
        f"{'below' if total_xg < 2.69 else 'above'} the 2.69 tournament average — "
        f"a {pace} match is expected."
    )
    if b["scorelines"]:
        top = b["scorelines"][0]
        parts.append(f"Most likely scoreline: {top['scoreline']} ({top['probability_pct']}%).")
    parts.append(f"Pattern-based upset probability: {b['upset_pct']:.0f}%.")
    return " ".join(parts)


def generate_post_match_review(b) -> str:
    """English post-match review comparing prediction to result."""
    t1, t2 = b["team1"], b["team2"]
    parts = []
    if b["scorelines"]:
        top = b["scorelines"][0]
        parts.append(
            f"Model's most likely scoreline was {top['scoreline']} "
            f"({top['probability_pct']}%); actual result {b['actual_score_plain']}."
        )
    fav = t1 if b["eg1"] >= b["eg2"] else t2
    parts.append(
        f"The expected-goals edge pointed to {fav} "
        f"({b['eg1']:.2f} vs {b['eg2']:.2f})."
    )
    if b.get("prediction_correct") is True:
        parts.append(f"Predicted outcome ({b['predicted_winner']}) was correct.")
    elif b.get("prediction_correct") is False:
        parts.append(f"Predicted outcome ({b['predicted_winner']}) missed.")
    expected_total = round(b["eg1"] + b["eg2"])
    actual_total = b.get("actual_total_goals")
    if actual_total is not None:
        if actual_total > expected_total:
            parts.append("Goal count exceeded model expectation.")
        elif actual_total < expected_total:
            parts.append("Goal count fell below model expectation.")
        else:
            parts.append("Goal count matched model expectation.")
    return " ".join(parts)


TEAM_FLAGS = {
    "Belgium": "🇧🇪", "Egypt": "🇪🇬", "Germany": "🇩🇪", "Curaçao": "🇨🇼",
    "Netherlands": "🇳🇱", "Japan": "🇯🇵", "Sweden": "🇸🇪", "Tunisia": "🇹🇳",
    "Spain": "🇪🇸", "Cabo Verde": "🇨🇻", "Saudi Arabia": "🇸🇦", "Uruguay": "🇺🇾",
    "Brazil": "🇧🇷", "Morocco": "🇲🇦", "France": "🇫🇷", "Argentina": "🇦🇷",
    "England": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "Portugal": "🇵🇹", "USA": "🇺🇸", "Mexico": "🇲🇽",
    "Canada": "🇨🇦", "Australia": "🇦🇺", "South Korea": "🇰🇷", "Czechia": "🇨🇿",
    "South Africa": "🇿🇦", "Qatar": "🇶🇦", "Switzerland": "🇨🇭",
    "Bosnia and Herzegovina": "🇧🇦", "Bosnia & Herzegovina": "🇧🇦",
    "Paraguay": "🇵🇾", "Türkiye": "🇹🇷",
    "Ivory Coast": "🇨🇮", "Côte d'Ivoire": "🇨🇮",
    "Ecuador": "🇪🇨", "Haiti": "🇭🇹", "Scotland": "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
    "Iran": "🇮🇷", "Iraq": "🇮🇶", "Jordan": "🇯🇴", "Uzbekistan": "🇺🇿",
    "Algeria": "🇩🇿", "Senegal": "🇸🇳", "Ghana": "🇬🇭",
    "Norway": "🇳🇴", "Austria": "🇦🇹", "Croatia": "🇭🇷", "Poland": "🇵🇱",
    "Colombia": "🇨🇴", "Venezuela": "🇻🇪", "Chile": "🇨🇱", "Peru": "🇵🇪",
    "DR Congo": "🇨🇩", "Cameroon": "🇨🇲", "Nigeria": "🇳🇬", "New Zealand": "🇳🇿",
}


EST = timezone(timedelta(hours=-5))   # fixed UTC-5 per spec (note: June is EDT/UTC-4)


def _flag(name):
    # Returns the flag emoji, or "" when unknown. (Browsers on Windows render
    # flag emoji as the two-letter code unless an emoji image font like Twemoji
    # is loaded — see base.html.)
    return TEAM_FLAGS.get(name, "")


def _est_dt(iso):
    """Parse a UTC kickoff string and convert to EST (fixed UTC-5)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EST)


def _est_time(iso):
    dt = _est_dt(iso)
    return dt.strftime("%H:%M") if dt else ""


def generate_model_edge(b) -> dict:
    """Identify the strongest betting-decision edge from the model output.

    `draw_edge` compares the model draw probability to a 26% baseline (typical
    World Cup group-stage draw rate). Advisory only — the operator decides.
    """
    draw_edge = b["pdraw"] - 26
    total_xg = b["eg1"] + b["eg2"]
    under_lean = total_xg < 2.5

    if draw_edge > 5:
        suggested = f"DRAW — {draw_edge:.0f}pp above market average"
    elif under_lean and total_xg < 2.2:
        suggested = f"UNDER 2.5 — total xG {total_xg:.2f}"
    elif b["p1"] > 55:
        suggested = f"{b['team1']} WIN — {b['p1']}% model probability"
    elif b["p2"] > 55:
        suggested = f"{b['team2']} WIN — {b['p2']}% model probability"
    else:
        suggested = "No clear edge identified"

    return {
        "suggested_bet": suggested,
        "draw_edge": round(draw_edge, 1),
        "total_xg": round(total_xg, 2),
        "under_lean": under_lean,
    }


def _season_record():
    """2026 prediction accuracy: how many saved predictions called the winner."""
    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT p.match_id AS mid, p.home_win_pct AS hp, p.draw_pct AS dp,
                   p.away_win_pct AS ap, m.home_score AS hs, m.away_score AS as_
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            WHERE m.season = 2026 AND m.status = 'completed'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY p.match_id, p.created_at
        """).fetchall()
    except Exception:
        return {"correct": 0, "total": 0, "pct": 0}
    finally:
        conn.close()

    seen, correct, total = set(), 0, 0
    for r in rows:
        if r["mid"] in seen:
            continue
        seen.add(r["mid"])
        hp, dp, ap = float(r["hp"]), float(r["dp"]), float(r["ap"])
        hs, as_ = r["hs"], r["as_"]
        actual = "home" if hs > as_ else ("away" if hs < as_ else "draw")
        pred = max([(hp, "home"), (dp, "draw"), (ap, "away")], key=lambda x: x[0])[1]
        total += 1
        if pred == actual:
            correct += 1
    return {"correct": correct, "total": total,
            "pct": round(correct / total * 100) if total else 0}


def _build_pre_match_bundle(match_id, conn=None) -> Optional[dict]:
    """Render-ready bundle for the MATCHIQ match page (pre + post modes).

    W/D/L, expected goals, scorelines and group situation come from
    DixonColesEngine; ELO is kept for the rating display. Returns None if the
    match is unknown.
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

        t1 = (match.get("home_team") or {}).get("name", "Team 1")
        t2 = (match.get("away_team") or {}).get("name", "Team 2")
        group_name = (match.get("group") or {}).get("name")
        kickoff = match.get("datetime")
        status = (match.get("status") or "").lower()
        is_post = status in {"completed", "ft", "finished"}

        elo1 = _elo.get_rating(t1)
        elo2 = _elo.get_rating(t2)

        # Dixon-Coles prediction (group situation + player xG + user notes).
        home_notes, away_notes = _load_user_notes(conn, match_id)
        dc = DixonColesEngine()
        pred = dc.predict_from_db(
            t1, t2, home_elo=elo1, away_elo=elo2,
            group_name=group_name, home_notes=home_notes, away_notes=away_notes,
            top_n=10,
        )
        wdl = pred["win_draw_loss"]
        eg = pred["expected_goals"]
        mtx = pred["matrix"]

        # Upset probability via pattern matcher.
        home_recent = _recent_team_stats(conn, t1)
        away_recent = _recent_team_stats(conn, t2)
        xg_diff = round(home_recent["xg_total"] - away_recent["xg_total"], 2)
        upset_pct = _pattern_matcher.calculate_upset_probability(
            _pattern_matcher.find_similar_matches({
                "xg_diff": xg_diff,
                "elo_diff": elo1 - elo2,
                "possession_diff": home_recent["possession_pct"] - away_recent["possession_pct"],
                "shots_diff": home_recent["shots_total"] - away_recent["shots_total"],
                "fatigue_diff": 0,
                "elimination_pressure": 0,
            }, top_n=3)
        )

        # Team strength cards.
        strengths = _team_strengths_from_db()
        s1 = dc._resolve_strength(t1, strengths) or {"attack": 1.0, "defense": 1.0}
        s2 = dc._resolve_strength(t2, strengths) or {"attack": 1.0, "defense": 1.0}
        xf1, xa1 = _match_strength(conn, dc._resolve_team_name(t1))
        xf2, xa2 = _match_strength(conn, dc._resolve_team_name(t2))

        group_label = (group_name or "Knockout").upper()
        md = _matchday(conn, group_name, kickoff)
        time_str = _est_time(kickoff)
        ctx_parts = [group_label]
        if md:
            ctx_parts.append(f"MATCHDAY {md}")
        if is_post:
            ctx_parts.append("FULL TIME")
        elif time_str:
            ctx_parts.append(f"{time_str} EST")
        ctx_line = " · ".join(ctx_parts)

        scorelines = pred["top_scorelines"][:5]
        top_pct = scorelines[0]["probability_pct"] if scorelines else 1

        b = {
            "match":     match,
            "team1":     t1,
            "team2":     t2,
            "flag1":     _flag(t1),
            "flag2":     _flag(t2),
            "code1":     _team_code(t1),
            "code2":     _team_code(t2),
            "ctx_line":  ctx_line,
            "elo_diff":  round(elo1 - elo2),
            "p1":        round(wdl["home_win"]),
            "pdraw":     round(wdl["draw"]),
            "p2":        round(wdl["away_win"]),
            "eg1":       eg["home"],
            "eg2":       eg["away"],
            "rho":       dc.RHO,
            "p00":       mtx.get((0, 0), 0.0),
            "p10":       mtx.get((1, 0), 0.0),
            "p11":       mtx.get((1, 1), 0.0),
            "scorelines": scorelines,
            "scoreline_matrix": scorelines,
            "top_pct": top_pct,
            "situation": pred.get("situation"),
            "upset_pct": upset_pct,
            "model_used": "Dixon-Coles v1",
            "strength1": {"elo": round(elo1), "xg_for": xf1, "xg_against": xa1,
                          "attack": s1["attack"], "defense": s1["defense"]},
            "strength2": {"elo": round(elo2), "xg_for": xf2, "xg_against": xa2,
                          "attack": s2["attack"], "defense": s2["defense"]},
        }
        b["narrative_pre"] = generate_pre_match_narrative(b)
        b["model_edge"] = generate_model_edge(b)

        if is_post:
            b["season_record"] = _season_record()
            # Score / actual winner are pure computations (always safe).
            hs = match.get("home_score") or 0
            as_ = match.get("away_score") or 0
            b["score"] = f"{hs} — {as_}"
            b["actual_score_plain"] = f"{hs}-{as_}"
            b["actual_total_goals"] = hs + as_
            actual_winner = t1 if hs > as_ else (t2 if hs < as_ else "Draw")
            b["actual_winner"] = actual_winner

            # Saved pre-kickoff prediction (DB). Fall back to the current model
            # if the lookup fails so a completed match never 500s; log the cause.
            prow = None
            try:
                prow = conn.execute(
                    "SELECT home_win_pct, draw_pct, away_win_pct FROM predictions "
                    "WHERE match_id = ? ORDER BY created_at",
                    (match_id,),
                ).fetchone()
            except Exception as e:
                print(f"[match] predictions lookup failed for {match_id}: {e}",
                      file=sys.stderr, flush=True)

            if prow:
                pp1, ppd, pp2 = prow["home_win_pct"], prow["draw_pct"], prow["away_win_pct"]
                has_saved = True
            else:
                pp1, ppd, pp2 = wdl["home_win"], wdl["draw"], wdl["away_win"]
                has_saved = False
            b["pred"] = {"p1": round(pp1), "pdraw": round(ppd), "p2": round(pp2)}

            predicted_winner = max(
                [(pp1, t1), (ppd, "Draw"), (pp2, t2)], key=lambda x: x[0]
            )[1]
            b["predicted_winner"] = predicted_winner
            b["prediction_correct"] = (predicted_winner == actual_winner)

            if has_saved:
                a1 = 1.0 if actual_winner == t1 else 0.0
                ad = 1.0 if actual_winner == "Draw" else 0.0
                a2 = 1.0 if actual_winner == t2 else 0.0
                b["brier"] = round(
                    (pp1 / 100 - a1) ** 2 + (ppd / 100 - ad) ** 2 + (pp2 / 100 - a2) ** 2, 2
                )
            else:
                b["brier"] = None
            b["narrative_post"] = generate_post_match_review(b)

        return b
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
    est_now = datetime.now(timezone.utc).astimezone(EST)
    today = est_now.date()
    today_str = today.isoformat()
    earliest_str = (today - timedelta(days=6)).isoformat()

    conn = database.get_connection()
    try:
        # Widen the UTC window by a day each side: a match's EST date can differ
        # from its UTC date near midnight, so we filter precisely in Python below.
        rows = conn.execute("""
            SELECT m.id AS id, ht.name AS t1, at.name AS t2,
                   m.kickoff_utc AS k, m.status AS status,
                   m.home_score AS hs, m.away_score AS as_, m.group_name AS grp
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = 2026
              AND substr(m.kickoff_utc, 1, 10) >= ?
              AND substr(m.kickoff_utc, 1, 10) <= ?
            ORDER BY m.kickoff_utc
        """, ((today - timedelta(days=7)).isoformat(),
              (today + timedelta(days=1)).isoformat())).fetchall()
    finally:
        conn.close()

    # Group fixtures by EST date for the sidebar; collect today's playable matches.
    by_date: dict = {}
    today_rows = []
    for r in rows:
        est = _est_dt(r["k"])
        if est is None:
            continue
        d = est.date().isoformat()
        if d < earliest_str or d > today_str:   # last 7 EST days only
            continue
        st = (r["status"] or "").lower()
        is_live = st in {"live", "in_progress"}
        is_done = st in {"completed", "ft", "finished"}
        time_str = est.strftime("%H:%M")
        if is_done:
            meta, state = f"FT {r['hs']}-{r['as_']}", "done"
        elif is_live:
            meta, state = "LIVE", "live"
        else:
            meta, state = f"{time_str} EST", ""
        by_date.setdefault(d, []).append({
            "id": r["id"],
            "teams": f"{_flag(r['t1'])} {r['t1']} vs {_flag(r['t2'])} {r['t2']}",
            "meta": meta, "state": state,
        })
        if d == today_str and not is_done:
            today_rows.append(r)

    def day_label(d):
        if d == today_str:
            return "TODAY"
        try:
            return date.fromisoformat(d).strftime("%b %d").upper()
        except ValueError:
            return d

    sidebar_days = [{"label": day_label(d), "fixtures": by_date[d]}
                    for d in sorted(by_date.keys(), reverse=True)]

    # Today's main cards with Dixon-Coles probabilities.
    dc = DixonColesEngine()
    today_cards = []
    for r in today_rows:
        t1, t2, grp = r["t1"], r["t2"], r["grp"]
        st = (r["status"] or "").lower()
        k = r["k"] or ""
        try:
            pred = dc.predict_from_db(
                t1, t2,
                home_elo=_elo.get_rating(t1), away_elo=_elo.get_rating(t2),
                group_name=grp or None,
            )
            wdl = pred["win_draw_loss"]
            eg = pred["expected_goals"]
            note = ((pred.get("situation") or {}).get("home") or {}).get("note")
            today_cards.append({
                "id": r["id"], "team1": t1, "team2": t2,
                "flag1": _flag(t1), "flag2": _flag(t2),
                "p1": round(wdl["home_win"]), "pdraw": round(wdl["draw"]), "p2": round(wdl["away_win"]),
                "xg1": eg["home"], "xg2": eg["away"], "note": note,
                "group": (grp or "").upper(),
                "time": _est_time(k),
                "is_live": st in {"live", "in_progress"},
            })
        except Exception as e:
            print(f"index: predict error for {t1} vs {t2}: {e}")

    return render_template(
        "index.html",
        sidebar_days=sidebar_days,
        today_cards=today_cards,
        today_label=today.strftime("%A, %B %d"),
        stage_label="GROUP STAGE",
    )


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
