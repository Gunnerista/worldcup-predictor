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
import math
import os
import random
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
    parts.append(f"Upset probability (model win chance for the ELO underdog): {b['upset_pct']:.0f}%.")
    return " ".join(parts)


def generate_post_match_review(b) -> dict:
    """Structured post-match review: prediction vs result, what the model got
    right / missed, and factors it couldn't capture."""
    t1, t2 = b["team1"], b["team2"]
    correct = bool(b.get("prediction_correct"))
    aw = b.get("actual_winner", "Draw")
    pw = b.get("predicted_winner", "Draw")
    eg1, eg2 = b["eg1"], b["eg2"]
    total_xg = eg1 + eg2
    actual_total = b.get("actual_total_goals", 0)
    pred = b.get("pred", {"p1": b["p1"], "pdraw": b["pdraw"], "p2": b["p2"]})

    # Predicted outcome summary.
    if pw == t1:
        label, pct = f"{t1} WIN", pred["p1"]
    elif pw == t2:
        label, pct = f"{t2} WIN", pred["p2"]
    else:
        label, pct = "DRAW", pred["pdraw"]
    top_sl = b["scorelines"][0]["scoreline"] if b["scorelines"] else "n/a"
    predicted = f"{label} ({pct}%) — most likely {top_sl}"
    actual = f"{t1} {b.get('actual_score_plain', '')} {t2}"

    # What the model got right.
    got_right = []
    if correct:
        got_right.append("Correct draw prediction" if aw == "Draw"
                         else f"Correct winner prediction ({aw} WIN)")
    fav = t1 if eg1 >= eg2 else t2
    if aw != "Draw" and aw == fav:
        got_right.append(f"xG edge correctly identified ({t1} {eg1:.2f} vs {t2} {eg2:.2f})")
    if total_xg < 2.5 and actual_total <= 2:
        got_right.append("Low-scoring prediction accurate")

    # What the model missed.
    missed = []
    if not correct:
        missed.append(f"Winner incorrect — predicted {pw}, actual {aw}")
    if b["scorelines"]:
        sl0 = b["scorelines"][0]
        predicted_total = sl0["home_goals"] + sl0["away_goals"]
        if actual_total - predicted_total >= 2:
            missed.append("Scoreline significantly underestimated")
        elif predicted_total - actual_total >= 2:
            missed.append("Scoreline significantly overestimated")
    if pw == "Draw" and aw != "Draw":
        missed.append("Draw probability overstated")

    # Factors the model could not capture.
    key_factors = []
    s1, s2 = b["strength1"], b["strength2"]
    if s1.get("xg_for") is None or s2.get("xg_for") is None:
        key_factors.append("No 2026 xG data yet — first match of tournament for this team")
    elo_diff = b.get("elo_diff", 0)
    stronger = t1 if elo_diff > 0 else t2
    if abs(elo_diff) >= 100 and aw != stronger:
        key_factors.append("High ELO gap upset — model underweighted upset probability")
    if total_xg > 0 and actual_total > 1.5 * total_xg:
        key_factors.append("Goal count significantly exceeded model expectation")
    if not key_factors:
        key_factors.append("No significant model blind spots identified")

    return {
        "predicted": predicted,
        "actual": actual,
        "correct": correct,
        "got_right": got_right,
        "missed": missed,
        "key_factors": key_factors,
    }


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


EASTERN = ZoneInfo("America/New_York")   # DST-aware: EST (UTC-5) winter, EDT (UTC-4) summer


def _flag(name):
    # Returns the flag emoji, or "" when unknown. (Browsers on Windows render
    # flag emoji as the two-letter code unless an emoji image font like Twemoji
    # is loaded — see base.html.)
    return TEAM_FLAGS.get(name, "")


def _est_dt(iso):
    """Parse a UTC kickoff string and convert to US Eastern (DST-aware)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN)


def _est_time(iso):
    dt = _est_dt(iso)
    return dt.strftime("%H:%M") if dt else ""


def _est_label(iso):
    """Eastern timezone abbreviation for the given instant ('EST' or 'EDT')."""
    dt = _est_dt(iso)
    return dt.tzname() if dt else "EST"


# ---------------------------------------------------------------------------
# Post-hoc calibration — temperature scaling of the engine's W/D/L distribution.
# T fitted by golden-section NLL minimisation on 439 recent international matches
# (2025-07..2026-05); see backtest_calibration.py. On that set, full-fit T=1.718
# improved ECE 0.083 -> 0.026 and RPS 0.194 -> 0.185 (the engine was overconfident
# at the tails). T>1 flattens the distribution. The transform is monotonic, so it
# is ARGMAX-PRESERVING: predicted_outcome and SEASON RECORD are unchanged — only
# the confidence magnitude softens. NOTE: validated only for the core engine path;
# the live-only stages (player_xG_adj, situation_mult) are not covered by this fit.
CALIBRATION_T = 1.718
CALIBRATION_VERSION = "v1.1+tcal"   # predictions.model_version cutover marker
# v1.1: sample-size strength shrinkage + raised λ cap (5.0) + model-derived upset
# probability (BUG1/3/4/5). Distributions saved from here differ from "v1+tcal";
# the bumped marker keeps pre/post-fix predictions distinguishable for analysis.
# Existing rows are never rewritten (the stored-snapshot integrity rule).


def calibrate_wdl(p_home, p_draw, p_away):
    """Temperature-scale a W/D/L percentage triple (0-100) via softmax(log(p)/T).
    Returns a calibrated triple (0-100, sums to 100). Order preserved (argmax
    invariant). Inputs may be percentages or fractions — softmax cancels the
    log-scale constant either way."""
    z = [math.log(max(float(p), 1e-9)) / CALIBRATION_T
         for p in (p_home, p_draw, p_away)]
    m = max(z)
    e = [math.exp(zi - m) for zi in z]
    s = sum(e)
    return tuple(100.0 * ei / s for ei in e)


def generate_prediction_label(b) -> dict:
    """Layer B: pure argmax labeling. The most likely W/D/L outcome IS the
    prediction — no market baseline, no asymmetric thresholds.

    A symmetric toss-up flag marks matches where the top two outcomes are within
    5 percentage points (== 0.05 on a probability scale). total_xg is carried as
    a neutral statistic only — it is NOT a betting lean and never sways the label.
    """
    t1, t2 = b["team1"], b["team2"]
    ranked = sorted(
        [("home", b["p1"], t1), ("draw", b["pdraw"], "Draw"), ("away", b["p2"], t2)],
        key=lambda x: x[1], reverse=True,
    )
    outcome, confidence, label = ranked[0]
    margin = ranked[0][1] - ranked[1][1]          # percentage points
    is_tossup = margin < 5.0                        # 5pp == 0.05, symmetric
    total_xg = b["eg1"] + b["eg2"]
    suggested = f"{label} ({round(confidence)}%)" + (" · toss-up" if is_tossup else "")

    return {
        "predicted_outcome": outcome,
        "label": label,
        "confidence": round(confidence, 1),
        "is_tossup": is_tossup,
        "margin": round(margin, 1),
        "total_xg": round(total_xg, 2),
        "suggested_bet": suggested,
        # draw_edge is intentionally gone (the 26% baseline was removed); the DB
        # column is preserved but written NULL for new predictions.
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
        # Layer B: the predicted outcome is the argmax of the stored distribution
        # (no market baseline, no asymmetric thresholds). Matches the match-page
        # labeling exactly, since both derive from the same distribution snapshot.
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
        # Temperature-calibrate the W/D/L distribution before display/labeling
        # (argmax-preserving; see calibrate_wdl). Applied to the live-compute path
        # only — POST snapshots read stored values verbatim.
        cwh, cwd, cwa = calibrate_wdl(wdl["home_win"], wdl["draw"], wdl["away_win"])
        eg = pred["expected_goals"]
        mtx = pred["matrix"]

        # Upset probability = the model's probability that the ELO underdog wins
        # (BUG5). The previous pattern-matcher k-NN ignored the ELO gap entirely —
        # its historical feature vectors hardcode elo_diff=0, so that dimension was
        # treated as uninformative — and its nearest neighbours for a favorite-vs-
        # underdog matchup were almost always non-upsets, collapsing the figure to
        # 0% for essentially every match. The model's own underdog win probability
        # is the honest, never-spuriously-zero answer and matches intuition
        # (e.g. Netherlands vs Sweden -> Sweden's win probability).
        upset_pct = round(cwa) if elo1 >= elo2 else round(cwh)

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
            ctx_parts.append(f"{time_str} {_est_label(kickoff)}")
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
            "p1":        round(cwh),
            "pdraw":     round(cwd),
            "p2":        round(cwa),
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
        # Knockout ties have no draw: surface a 2-way advance probability
        # (win + half the draw, the same split validated for the bracket).
        # group_name is None for every knockout fixture (groups only exist in
        # the group stage).
        b["is_knockout"] = group_name is None
        b["adv1"] = round(cwh + cwd / 2)
        b["adv2"] = 100 - b["adv1"]

        b["narrative_pre"] = generate_pre_match_narrative(b)
        b["prediction"] = generate_prediction_label(b)

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
                    "SELECT home_win_pct, draw_pct, away_win_pct, "
                    "suggested_bet, draw_edge, total_xg FROM predictions "
                    "WHERE match_id = ? ORDER BY created_at",
                    (match_id,),
                ).fetchone()
            except Exception as e:
                print(f"[match] predictions lookup failed for {match_id}: {e}",
                      file=sys.stderr, flush=True)

            # CORRECT/INCORRECT, Brier, and the structured review are only
            # meaningful when a real pre-kickoff prediction was saved. Without one
            # a post-result recompute would fabricate a "prediction" the model
            # never committed to, so there is nothing to score — the page shows
            # "No prediction recorded" instead.
            b["has_prediction"] = bool(prow)
            if prow:
                # Distribution snapshot (cast Decimal->float for PostgreSQL — PG
                # returns REAL columns as decimal.Decimal; see §11.8).
                pp1 = float(prow["home_win_pct"])
                ppd = float(prow["draw_pct"])
                pp2 = float(prow["away_win_pct"])
                b["pred"] = {"p1": round(pp1), "pdraw": round(ppd), "p2": round(pp2)}

                # POST-MATCH shows the PRE-KICKOFF odds (saved snapshot), not the
                # post-result recompute. Override the top probability bar.
                b["p1"], b["pdraw"], b["p2"] = round(pp1), round(ppd), round(pp2)

                # Layer B: the label is the argmax of the saved distribution
                # snapshot — always recomputable, identical for new and legacy
                # rows. The legacy suggested_bet/draw_edge columns are intentionally
                # NOT used to pick the winner anymore (that reintroduced the old
                # draw-biased framing). total_xg is read as a neutral statistic.
                txg = prow["total_xg"]
                snap = generate_prediction_label({
                    "team1": t1, "team2": t2,
                    "p1": pp1, "pdraw": ppd, "p2": pp2,
                    "eg1": 0.0, "eg2": 0.0,
                })
                snap["total_xg"] = float(txg) if txg is not None else None
                b["prediction"] = snap

                predicted_winner = (
                    t1 if snap["predicted_outcome"] == "home"
                    else t2 if snap["predicted_outcome"] == "away"
                    else "Draw"
                )
                b["predicted_winner"] = predicted_winner
                b["prediction_correct"] = (predicted_winner == actual_winner)

                a1 = 1.0 if actual_winner == t1 else 0.0
                ad = 1.0 if actual_winner == "Draw" else 0.0
                a2 = 1.0 if actual_winner == t2 else 0.0
                b["brier"] = round(
                    (pp1 / 100 - a1) ** 2 + (ppd / 100 - ad) ** 2 + (pp2 / 100 - a2) ** 2, 2
                )
                b["narrative_post"] = generate_post_match_review(b)
            else:
                b["brier"] = None
                # No saved snapshot: the top "Pre-Match Odds" bar must NEVER show
                # a post-result recompute (current ELO already reflects the result).
                # Null the odds so the template renders nothing instead.
                b["p1"] = b["pdraw"] = b["p2"] = None

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
    est_now = datetime.now(timezone.utc).astimezone(EASTERN)
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
            meta, state = f"{time_str} {est.tzname()}", ""
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
            # Calibrate W/D/L for the index cards too, so the homepage and the
            # match-detail PRE page show the same numbers (argmax-preserving).
            cwh, cwd, cwa = calibrate_wdl(wdl["home_win"], wdl["draw"], wdl["away_win"])
            eg = pred["expected_goals"]
            note = ((pred.get("situation") or {}).get("home") or {}).get("note")
            today_cards.append({
                "id": r["id"], "team1": t1, "team2": t2,
                "flag1": _flag(t1), "flag2": _flag(t2),
                "p1": round(cwh), "pdraw": round(cwd), "p2": round(cwa),
                "xg1": eg["home"], "xg2": eg["away"], "note": note,
                "group": (grp or "").upper(),
                "time": _est_time(k),
                "tz": _est_label(k),
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


def _build_reliability_svg(hero):
    """Map reliability bins (from calibration_report.json) to SVG geometry for the
    methodology page. Pure layout from data — no numbers are hardcoded here."""
    ML, MT, SIZE = 44, 16, 300

    def X(p):
        return round(ML + p * SIZE, 1)

    def Y(o):
        return round((MT + SIZE) - o * SIZE, 1)

    def series(points):
        poly = " ".join(f"{X(pt['pred'])},{Y(pt['obs'])}" for pt in points)
        nmax = max((pt["n"] for pt in points), default=1) or 1
        dots = [{"cx": X(pt["pred"]), "cy": Y(pt["obs"]),
                 "r": round(2 + (pt["n"] / nmax) ** 0.5 * 4, 1)} for pt in points]
        return {"poly": poly, "dots": dots}

    ticks = [0.0, 0.25, 0.5, 0.75, 1.0]
    return {
        "diag": {"x1": X(0), "y1": Y(0), "x2": X(1), "y2": Y(1)},
        "raw": series(hero["raw"]),
        "cal": series(hero["calibrated"]),
        "xticks": [{"x": X(t), "y": MT + SIZE, "label": f"{t:.2f}"} for t in ticks],
        "yticks": [{"x": ML, "y": Y(t), "label": f"{t:.2f}"} for t in ticks],
        "ml": ML, "mt": MT, "size": SIZE, "y0": MT + SIZE,
    }


@app.route("/methodology")
def methodology():
    """Model evaluation & calibration page. All numbers come from the committed
    artifact static/calibration_report.json (regenerated by backtest_calibration.py);
    nothing is hardcoded here."""
    path = os.path.join(app.static_folder, "calibration_report.json")
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        return render_template("methodology.html", report=None, curve=None)
    curve = _build_reliability_svg(report["hero"])
    return render_template("methodology.html", report=report, curve=curve)


@app.route("/api/brier")
def api_brier():
    """Model calibration: Brier score over completed, predicted 2026 matches."""
    res = compute_brier_score(season=2026)
    return {"brier_score": res["brier_score"], "n_matches": res["n_matches"]}


# --- Knockout bracket: Monte Carlo over the confirmed R32 bracket ----------
# Bracket wiring verified from the BALLDONTLIE source labels ("W##" = winner of
# official match ##). R32 ties map to match numbers 73..88 in kickoff order;
# R16/QF/SF feed forward from those. Final = winner(101) vs winner(102).
_KO_R16 = {89: (73, 75), 90: (74, 77), 91: (76, 78), 92: (79, 80),
           93: (83, 84), 94: (81, 82), 95: (86, 88), 96: (85, 87)}
_KO_QF = {97: (89, 90), 98: (93, 94), 99: (91, 92), 100: (95, 96)}
_KO_SF = {101: (97, 98), 102: (99, 100)}
_KO_ITERS = 20000
_knockout_cache_result = None


def _ko_p_advance(dc, x, y, cache, inp):
    """P(x advances) in a neutral single-leg tie = win + 0.5*draw (calibrated).
    No extra-time/penalty modelling: the draw is split 50/50. Validated on
    2018/2022 knockout (Brier 0.2216) and consistent with the literature that
    shootouts are ~coin-flips. Pure team data (player adjustment is neutral).

    inp[team] = (attack*player_adj, defense, elo) is preloaded ONCE per team so
    the Monte Carlo never hits the DB — predict_from_db would re-query strengths
    on every matchup, which on PostgreSQL means hundreds of round-trips and a
    gunicorn worker timeout (the page 500'd; SQLite was just fast enough to hide
    it). Here we reuse the same _expected_goals/_assemble path with cached inputs."""
    key = (x, y)
    if key in cache:
        return cache[key]
    ha, hd, he = inp[x]
    aa, ad, ae = inp[y]
    lam, mu = dc._expected_goals(he, ae, ha, hd, aa, ad, True)
    w = dc._assemble(lam, mu, 1)["win_draw_loss"]
    cwh, cwd, cwa = calibrate_wdl(w["home_win"], w["draw"], w["away_win"])
    p = (cwh + 0.5 * cwd) / 100.0
    cache[key] = p
    cache[(y, x)] = 1.0 - p   # neutral tie is symmetric
    return p


def _compute_knockout(iters=_KO_ITERS, seed=42):
    """Monte Carlo the confirmed R32 bracket -> round-by-round + champion
    probabilities. Completed ties are FIXED to their real winner; only
    undecided ties are simulated, so the board reflects the live state."""
    _ensure_engines()
    dc = DixonColesEngine()

    conn = database.get_connection()
    try:
        rows = conn.execute("""
            SELECT m.id AS id, ht.name AS h, at.name AS a, m.status AS status,
                   m.home_score AS hs, m.away_score AS as_
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = 2026 AND m.stage = 'Round of 32'
            ORDER BY m.kickoff_utc
        """).fetchall()
    finally:
        conn.close()

    # R32 ties in kickoff order == official match numbers 73..88.
    r32 = []
    for r in rows:
        h, a = r["h"], r["a"]
        st = (r["status"] or "").lower()
        winner = None
        if st in {"completed", "ft", "finished"}:
            hs, as_ = r["hs"], r["as_"]
            if hs is not None and as_ is not None and hs != as_:
                winner = h if hs > as_ else a
        r32.append((r["id"], h, a, winner))

    teams = [t for (_, h, a, _) in r32 for t in (h, a)]

    # Preload per-team model inputs ONCE: (attack*player_adj, defense, elo).
    # Keeps the Monte Carlo and bracket DB-free (the PG timeout fix above).
    strengths = _team_strengths_from_db()

    def _team_inputs(name):
        s = dc._resolve_strength(name, strengths) or {"attack": 1.0, "defense": 1.0}
        padj = float(dc._player_xg_adjustment(name))
        return (float(s["attack"]) * padj, float(s["defense"]),
                float(_elo.get_rating(name)))

    inp = {t: _team_inputs(t) for t in teams}

    cache = {}
    rng = random.Random(seed)

    def beats(x, y):
        return x if rng.random() < _ko_p_advance(dc, x, y, cache, inp) else y

    reach = {t: {"R16": 0, "QF": 0, "SF": 0, "F": 0, "CHAMP": 0} for t in teams}

    for _ in range(iters):
        win = {}
        for i, (_id, h, a, fixed) in enumerate(r32):
            w = fixed if fixed else beats(h, a)
            win[73 + i] = w
            reach[w]["R16"] += 1
        for num, (x, y) in _KO_R16.items():
            w = beats(win[x], win[y]); win[num] = w; reach[w]["QF"] += 1
        for num, (x, y) in _KO_QF.items():
            w = beats(win[x], win[y]); win[num] = w; reach[w]["SF"] += 1
        for num, (x, y) in _KO_SF.items():
            w = beats(win[x], win[y]); win[num] = w; reach[w]["F"] += 1
        champ = beats(win[101], win[102])
        reach[champ]["CHAMP"] += 1

    board = []
    for t in sorted(teams, key=lambda t: -reach[t]["CHAMP"]):
        r = reach[t]
        board.append({
            "team": t, "flag": _flag(t),
            "r16": round(r["R16"] / iters * 100, 1),
            "qf": round(r["QF"] / iters * 100, 1),
            "sf": round(r["SF"] / iters * 100, 1),
            "final": round(r["F"] / iters * 100, 1),
            "champ": round(r["CHAMP"] / iters * 100, 1),
        })

    # Modal ("most likely") bracket: at each tie the favorite advances. This is
    # the single most-probable path, used to label the visual bracket tree.
    def fav(x, y):
        return x if _ko_p_advance(dc, x, y, cache, inp) >= 0.5 else y

    modal = {}
    for i, (_id, h, a, fixed) in enumerate(r32):
        modal[73 + i] = fixed if fixed else fav(h, a)
    for num, (x, y) in _KO_R16.items():
        modal[num] = fav(modal[x], modal[y])
    for num, (x, y) in _KO_QF.items():
        modal[num] = fav(modal[x], modal[y])
    for num, (x, y) in _KO_SF.items():
        modal[num] = fav(modal[x], modal[y])

    # Visual leaf ordering so each column lines up vertically with the next
    # (standard bracket layout). expand() replaces each node with its feeders.
    def expand(nums, wiring):
        out = []
        for n in nums:
            out += list(wiring.get(n, (n,)))
        return out
    sf_order = [101, 102]
    qf_order = expand(sf_order, _KO_SF)         # [97,98,99,100]
    r16_order = expand(qf_order, _KO_QF)        # 8 R16 match numbers
    r32_order = expand(r16_order, _KO_R16)      # 16 R32 match numbers, visual order

    def reach_cell(team, key):
        return {"team": team, "flag": _flag(team),
                "pct": round(reach[team][key] / iters * 100)}

    # R32 column: real ties with advance %, in bracket order.
    r32_col = []
    for num in r32_order:
        _id, h, a, fixed = r32[num - 73]
        ph = round(_ko_p_advance(dc, h, a, cache, inp) * 100)
        r32_col.append({
            "id": _id, "done": fixed is not None, "winner": fixed,
            "top": {"team": h, "flag": _flag(h), "pct": ph,
                    "win": fixed == h},
            "bot": {"team": a, "flag": _flag(a), "pct": 100 - ph,
                    "win": fixed == a},
        })

    def feeder_col(order, wiring, key):
        col = []
        for num in order:
            x, y = wiring[num]
            col.append({"top": reach_cell(modal[x], key),
                        "bot": reach_cell(modal[y], key)})
        return col

    rounds = [
        {"name": "Round of 32", "kind": "r32", "matches": r32_col},
        {"name": "Round of 16", "kind": "feeder",
         "matches": feeder_col(r16_order, _KO_R16, "R16")},
        {"name": "Quarter-final", "kind": "feeder",
         "matches": feeder_col(qf_order, _KO_QF, "QF")},
        {"name": "Semi-final", "kind": "feeder",
         "matches": feeder_col(sf_order, _KO_SF, "SF")},
        {"name": "Final", "kind": "feeder",
         "matches": [{"top": reach_cell(modal[101], "F"),
                      "bot": reach_cell(modal[102], "F")}]},
    ]
    # Trophy = the team with the HIGHEST title odds (board is sorted by champ%),
    # so it always matches the board's #1 and the intuitive "who will win".
    # The bracket lines above still show the most-likely path, which can end on a
    # near-even rival; the trophy is the probability winner, not the modal path.
    champion = {"team": board[0]["team"], "flag": board[0]["flag"],
                "pct": board[0]["champ"]}

    return {"board": board, "rounds": rounds, "champion": champion,
            "iters": iters}


def _get_knockout():
    """Cached knockout result; recomputed lazily after the cache is cleared
    (the live-sync loop clears it when a result changes, alongside the ELO)."""
    global _knockout_cache_result
    if _knockout_cache_result is None:
        _knockout_cache_result = _compute_knockout()
    return _knockout_cache_result


@app.route("/knockout")
def knockout():
    """Knockout bracket: per-tie advance % + Monte Carlo champion board."""
    return render_template("knockout.html", **_get_knockout())


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
            eg = pred["expected_goals"]
            # Calibrate before labeling AND before persisting, so the stored
            # snapshot is the calibrated distribution. model_version marks the
            # cutover; pre-cutover rows ("v1"/"manual") are never converted.
            cwh, cwd, cwa = calibrate_wdl(wdl["home_win"], wdl["draw"], wdl["away_win"])
            label = generate_prediction_label({
                "team1": home, "team2": away,
                "p1": round(cwh), "pdraw": round(cwd),
                "p2": round(cwa),
                "eg1": eg["home"], "eg2": eg["away"],
            })
            save_prediction(r["id"], cwh, cwd, cwa,
                            model_version=CALIBRATION_VERSION, conn=conn,
                            suggested_bet=label["suggested_bet"],
                            draw_edge=None, total_xg=label["total_xg"],
                            predicted_outcome=label["predicted_outcome"],
                            confidence=label["confidence"],
                            is_tossup=1 if label["is_tossup"] else 0)
            saved += 1

        if saved:
            conn.commit()
        return saved
    finally:
        conn.close()


def _live_sync_loop() -> None:
    global _elo, _knockout_cache_result
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
                _knockout_cache_result = None   # bracket depends on ELO + results
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
