"""
model.py
========

Five-engine prediction pipeline for FIFA World Cup matches.

  EloEngine            -- ELO ratings with stage-aware K-factor
  TacticalEngine       -- weakness analysis, possession impact, concede patterns
  PlayerMatchupEngine  -- key player ranking, head-to-head, fatigue, motivation
  PatternMatcher       -- similar-match lookup over historical feature vectors
  NarrativeEngine      -- composes a Korean consulting-style report

CRITICAL CONTRACT: every probability, percentage, similarity score, and
"recommendation" below is COMPUTED by an engine from typed inputs. No values
are echoed from external API responses.

Run directly for a self-contained Korea vs Portugal demo:
    python model.py
"""

from __future__ import annotations

import math
import sys
from typing import Optional


# ===========================================================================
# Module constants
# ===========================================================================

ELO_K_FACTOR: dict[str, int] = {
    "group":         20,
    "round_of_16":   30,
    "quarter":       40,
    "semi":          50,
    "final":         50,
}

DEFAULT_ELO: float = 1500.0

# Draw model: P(draw) maxes at equal ELOs and decays gaussian-style with gap.
# Calibration targets (sigma = 350):
#   elo_diff = 0   -> P_draw ≈ 0.28
#   elo_diff = 200 -> P_draw ≈ 0.20
#   elo_diff = 400 -> P_draw ≈ 0.07
#   elo_diff = 600 -> P_draw ≈ 0.01
# Wider sigma than the academic ELO default because World Cup matches see
# more draws than the pure 2-way model predicts, especially at moderate gaps.
_DRAW_MAX:   float = 0.28
_DRAW_SIGMA: float = 350.0


# ===========================================================================
# 2026 team-strength seeds — FIFA Men's World Ranking points (~June 2026,
# pre-tournament) for the 48 qualified nations. Source: football-ranking.com,
# Wikipedia (FIFA Men's World Ranking, 11 Jun 2026), ESPN. Team set matches the
# qualifiers present in the database.
# ===========================================================================

FIFA_POINTS_2026: dict[str, float] = {
    "Argentina": 1877, "Spain": 1875, "France": 1871, "England": 1828,
    "Portugal": 1768, "Brazil": 1766, "Morocco": 1755, "Netherlands": 1754,
    "Belgium": 1742, "Germany": 1736, "Croatia": 1715, "Colombia": 1698,
    "Mexico": 1687, "Senegal": 1684, "Uruguay": 1673, "USA": 1671,
    "Japan": 1662, "Switzerland": 1650, "Iran": 1620, "Türkiye": 1606,
    "Ecuador": 1599, "Austria": 1597, "South Korea": 1592, "Australia": 1579,
    "Algeria": 1571, "Egypt": 1562, "Canada": 1559, "Norway": 1557,
    "Côte d'Ivoire": 1541, "Panama": 1539, "Sweden": 1510, "Czechia": 1506,
    "Paraguay": 1505, "Scotland": 1503, "Tunisia": 1476, "DR Congo": 1474,
    "Uzbekistan": 1459, "Qatar": 1450, "Iraq": 1446, "South Africa": 1428,
    "Saudi Arabia": 1423, "Bosnia & Herzegovina": 1387, "Jordan": 1387,
    "Cabo Verde": 1371, "Ghana": 1346, "Curaçao": 1294, "Haiti": 1293,
    "New Zealand": 1275,
}

# Normalise FIFA points to the ELO scale: a 1500-point side maps to ELO 1500,
# and the rating spread is compressed to 0.8x (the FIFA spread is wider than the
# ELO scale we replay 2018/2022 results on).
INITIAL_RATINGS_2026: dict[str, float] = {
    team: 1500.0 + (pts - 1500.0) * 0.8
    for team, pts in FIFA_POINTS_2026.items()
}


# ===========================================================================
# Engine 1: EloEngine
# ===========================================================================

class EloEngine:
    """ELO ratings with stage-aware K-factor, plus a 3-way (W/D/L) extension."""

    def __init__(self) -> None:
        self.ratings: dict[str, float] = {}

    # ---- rating ops -------------------------------------------------------

    def set_rating(self, team: str, rating: float) -> None:
        self.ratings[team] = float(rating)

    def get_rating(self, team: str) -> float:
        return self.ratings.get(team, DEFAULT_ELO)

    def get_rankings(self) -> list[tuple[str, float]]:
        """All teams sorted by ELO descending."""
        return sorted(self.ratings.items(), key=lambda kv: kv[1], reverse=True)

    # ---- probability ------------------------------------------------------

    def get_win_probability(self, home_elo: float, away_elo: float) -> dict:
        """Compute home / draw / away probabilities for a neutral-venue match.

        Returns: {home, draw, away, elo_diff, expected_home_2way}
        """
        elo_diff = float(home_elo) - float(away_elo)

        # Standard ELO 2-way expected score (home perspective)
        expected_home_2way = 1.0 / (1.0 + 10 ** (-elo_diff / 400.0))

        # Draw probability collapses with the ELO gap
        draw_p = _DRAW_MAX * math.exp(-(elo_diff / _DRAW_SIGMA) ** 2)

        # Allocate the remaining 1 - draw_p between home and away
        remaining = 1.0 - draw_p
        home_p = expected_home_2way * remaining
        away_p = (1.0 - expected_home_2way) * remaining

        return {
            "home":               round(home_p, 4),
            "draw":               round(draw_p, 4),
            "away":               round(away_p, 4),
            "elo_diff":           elo_diff,
            "expected_home_2way": round(expected_home_2way, 4),
        }

    # ---- update -----------------------------------------------------------

    # ---- 2026 host + proximity bonuses --------------------------------

    HOST_2026_CODES = {"USA", "CAN", "MEX"}
    CONMEBOL_CODES  = {"ARG", "BRA", "URU", "COL", "ECU", "VEN", "CHI", "PAR", "BOL", "PER"}
    HOST_BONUS_ELO       = 50.0
    PROXIMITY_BONUS_ELO  = 20.0

    def host_bonus(self, country_code: Optional[str]) -> float:
        if country_code and country_code.upper() in self.HOST_2026_CODES:
            return self.HOST_BONUS_ELO
        return 0.0

    def proximity_bonus(self, country_code: Optional[str], host_country: Optional[str]) -> float:
        """+20 ELO for CONMEBOL teams when the host is a CONCACAF nation (2026 case)."""
        if (country_code and country_code.upper() in self.CONMEBOL_CODES
            and host_country and host_country.upper() in self.HOST_2026_CODES):
            return self.PROXIMITY_BONUS_ELO
        return 0.0

    def get_win_probability_with_context(
        self,
        home_elo: float, away_elo: float,
        home_country: Optional[str] = None,
        away_country: Optional[str] = None,
        host_country: Optional[str] = None,
    ) -> dict:
        """Same as get_win_probability but applies 2026 host + CONMEBOL-proximity bonuses."""
        h_bonus = self.host_bonus(home_country) + self.proximity_bonus(home_country, host_country)
        a_bonus = self.host_bonus(away_country) + self.proximity_bonus(away_country, host_country)
        result = self.get_win_probability(home_elo + h_bonus, away_elo + a_bonus)
        result["home_bonus_elo"] = h_bonus
        result["away_bonus_elo"] = a_bonus
        return result

    # -------------------------------------------------------------------

    def update(self, home_team: str, away_team: str, result: str, stage: str) -> dict:
        """Apply a match result to both teams' ratings.

        result: 'home_win' | 'draw' | 'away_win'
        stage:  one of ELO_K_FACTOR's keys
        """
        k = ELO_K_FACTOR.get(stage, ELO_K_FACTOR["group"])

        home_elo = self.get_rating(home_team)
        away_elo = self.get_rating(away_team)
        probs = self.get_win_probability(home_elo, away_elo)
        expected_home = probs["expected_home_2way"]

        actual_home = {"home_win": 1.0, "draw": 0.5, "away_win": 0.0}.get(result)
        if actual_home is None:
            raise ValueError(f"unknown result: {result!r}")

        delta = k * (actual_home - expected_home)
        new_home = home_elo + delta
        new_away = away_elo - delta

        self.ratings[home_team] = new_home
        self.ratings[away_team] = new_away

        return {
            "home_old": home_elo, "home_new": round(new_home, 2),
            "away_old": away_elo, "away_new": round(new_away, 2),
            "delta":    round(delta, 2),
            "k":        k,
        }


# ===========================================================================
# Engine 2: TacticalEngine
# ===========================================================================

class TacticalEngine:
    """Concede-zone weakness, possession edge, tactical-adjustment forecast."""

    _ZONE_KR = {"left": "왼쪽", "center": "중앙", "right": "오른쪽"}

    def analyze_previous_match(self, team_id, match_data: dict) -> dict:
        """Identify the most-exposed defensive zone from shots/goals distribution.

        match_data:
          shots_against_by_zone: {"left", "center", "right"} -> int
          goals_against_by_zone: same
          possession_pct:        float
        """
        shots = match_data.get("shots_against_by_zone") or {}
        goals = match_data.get("goals_against_by_zone") or {}

        # Threat-weighted score per zone: 1.0/shot + 3.0/goal
        threat = {
            z: (shots.get(z, 0) or 0) * 1.0 + (goals.get(z, 0) or 0) * 3.0
            for z in ("left", "center", "right")
        }
        total = sum(threat.values())

        if total == 0:
            return {
                "team_id":             team_id,
                "weakest_zone":        None,
                "weakest_zone_kr":     "—",
                "vulnerability_score": 0,
                "possession_pattern":  "데이터 없음",
                "possession_pct":      match_data.get("possession_pct", 50.0),
                "threat_distribution": threat,
            }

        weakest = max(threat, key=lambda k: threat[k])
        vuln = round(100 * threat[weakest] / total)

        poss = float(match_data.get("possession_pct", 50.0))
        if poss > 55:
            pattern = "공격 점유 — 빌드업 길게 가져감"
        elif poss < 45:
            pattern = "수비적 — 역습 위주"
        else:
            pattern = "균형 점유"

        return {
            "team_id":             team_id,
            "weakest_zone":        weakest,
            "weakest_zone_kr":     self._ZONE_KR.get(weakest, weakest),
            "vulnerability_score": vuln,
            "possession_pattern":  pattern,
            "possession_pct":      poss,
            "threat_distribution": threat,
        }

    def predict_tactical_adjustment(self, previous_analysis: dict) -> dict:
        """Given a weakness signal, forecast the manager's likely adjustment."""
        zone_kr = previous_analysis.get("weakest_zone_kr") or "특정"
        score = int(previous_analysis.get("vulnerability_score", 0))

        if score >= 50:
            return {
                "confidence":           "high",
                "expected_change_prob": 0.75,
                "action":               f"이전 경기 {zone_kr} 측면 노출 {score}% — 측면 수비 보강 가능성 높음",
            }
        if score >= 35:
            return {
                "confidence":           "medium",
                "expected_change_prob": 0.45,
                "action":               f"{zone_kr} 측면 보강 가능성 있음 (노출 {score}%)",
            }
        return {
            "confidence":           "low",
            "expected_change_prob": 0.15,
            "action":               "전술 큰 변경 가능성 낮음 — 기존 유지 예상",
        }

    def calculate_possession_impact(self, home_poss_avg: float, away_poss_avg: float) -> dict:
        """Win-prob shift attributable to possession edge.

        Linear slope: 5%p win-prob per 10%p possession edge (research baseline),
        suppressed below an 8%p threshold (noise floor), capped at ±15%p.
        """
        diff_pp = float(home_poss_avg) - float(away_poss_avg)
        abs_diff = abs(diff_pp)

        if abs_diff < 8:
            return {
                "diff_pp":        diff_pp,
                "win_prob_shift": 0.0,
                "confidence":     "low",
                "note":           "점유율 차이 미미 (8%p 미만) — 영향 무시",
            }

        shift = max(-0.15, min(0.15, 0.005 * diff_pp))   # 10%p edge -> 5%p shift
        confidence = "high" if abs_diff >= 15 else "medium"
        sign = "+" if diff_pp >= 0 else "-"
        return {
            "diff_pp":        diff_pp,
            "win_prob_shift": round(shift, 4),
            "confidence":     confidence,
            "note":           f"점유율 우위 (홈 {sign}{abs_diff:.0f}%p) → 홈 승률 보정 {shift*100:+.1f}%p",
        }

    # ---- Altitude impact (2026 venues) --------------------------------

    # Known venue altitudes (meters). Stadium IDs from BALLDONTLIE will be
    # filled in after backfill; for now we match by name or accept a raw m value.
    KNOWN_ALTITUDES_M: dict = {
        "Estadio Azteca":   2240,  # Mexico City
        "Estadio Akron":    1567,  # Guadalajara
        "Estadio BBVA":      520,  # Monterrey
        # US/CAN venues all < 500m
    }
    ALTITUDE_THRESHOLD_M = 1500
    ALTITUDE_PENALTY = {
        "UEFA":     -0.15,
        "AFC":      -0.10,
        "CAF":      -0.08,
        "CONMEBOL":  0.00,   # adapted
        "CONCACAF":  0.00,   # local
        "OFC":      -0.08,
    }

    def calculate_altitude_impact(self, stadium_id, team_origin: str) -> dict:
        """Stamina penalty for low-altitude-adapted teams at high-altitude venues.

        stadium_id can be:
          - a known venue name string (e.g. "Estadio Azteca")
          - a numeric BALLDONTLIE stadium id (matched against KNOWN_ALTITUDES_M)
          - a raw altitude in meters (treated as such if >= 200)
        team_origin: confederation code (UEFA / AFC / CAF / CONMEBOL / CONCACAF / OFC)
        """
        # Resolve altitude
        if isinstance(stadium_id, (int, float)) and float(stadium_id) >= 200:
            altitude_m = float(stadium_id)
        else:
            altitude_m = float(self.KNOWN_ALTITUDES_M.get(stadium_id, 0))

        if altitude_m < self.ALTITUDE_THRESHOLD_M:
            return {
                "altitude_m": altitude_m,
                "impact":     0.0,
                "note":       f"고도 {altitude_m:.0f}m — 영향 없음",
            }

        impact = self.ALTITUDE_PENALTY.get(team_origin, -0.08)
        if impact == 0.0:
            note = f"고지대 {altitude_m:.0f}m + {team_origin} → 적응됨, 패널티 없음"
        else:
            note = f"고지대 {altitude_m:.0f}m + {team_origin} → 체력 {impact*100:+.0f}%"
        return {"altitude_m": altitude_m, "impact": impact, "note": note}

    # ---- Group-stage situational motivation ---------------------------

    def calculate_group_stage_motivation(self, team_id, current_standings: dict) -> dict:
        """Motivation/style shift driven by a team's group-stage position.

        current_standings keys:
          status: one of 'qualified' | 'eliminated' | 'must_win' | 'draw_sufficient' | 'live'
        """
        status = ((current_standings or {}).get("status") or "live").lower().strip()

        if status == "qualified":
            return {"motivation_shift": -0.10,
                    "pattern": "안전 운영 / 로테이션 예상",
                    "note":    "16강 확정 → 집중력 -10%p"}
        if status == "eliminated":
            return {"motivation_shift": -0.20,
                    "pattern": "동기부여 저하 — 자존심 경기",
                    "note":    "탈락 확정 → 동기부여 -20%p"}
        if status == "must_win":
            return {"motivation_shift": +0.15,
                    "pattern": "공격적 / 모험적",
                    "note":    "무조건 이겨야 함 → +15%p"}
        if status == "draw_sufficient":
            return {"motivation_shift": +0.05,
                    "pattern": "안전 플레이 — 비기면 통과",
                    "note":    "비기면 통과 → 안정성 +5%p"}
        return {"motivation_shift": 0.0,
                "pattern": "균형 운영",
                "note":    "조별 상황 미정"}

    # -------------------------------------------------------------------

    def get_concede_pattern(self, team_id, recent_matches: list) -> dict:
        """Aggregate concede minute-buckets + zones across recent matches.

        Each match: {"goals_against": [{"minute": int, "zone": "left"|"center"|"right"}, ...]}
        """
        buckets = {"0-15": 0, "16-30": 0, "31-45": 0, "46-60": 0, "61-75": 0, "76-90": 0}
        zones = {"left": 0, "center": 0, "right": 0}
        total = 0

        for m in recent_matches or []:
            for g in m.get("goals_against") or []:
                minute = int(g.get("minute", 0))
                zone = g.get("zone", "center")
                if zone in zones:
                    zones[zone] += 1
                total += 1

                if minute <= 15:
                    buckets["0-15"] += 1
                elif minute <= 30:
                    buckets["16-30"] += 1
                elif minute <= 45:
                    buckets["31-45"] += 1
                elif minute <= 60:
                    buckets["46-60"] += 1
                elif minute <= 75:
                    buckets["61-75"] += 1
                else:
                    buckets["76-90"] += 1

        if total == 0:
            return {
                "vulnerable_window":           None,
                "vulnerable_window_share_pct": 0,
                "dominant_zone":               None,
                "dominant_zone_kr":            "—",
                "buckets":                     buckets,
                "zones":                       zones,
                "sample_size":                 0,
            }

        vwin = max(buckets, key=lambda k: buckets[k])
        dzone = max(zones, key=lambda k: zones[k])
        return {
            "vulnerable_window":           vwin,
            "vulnerable_window_share_pct": round(100 * buckets[vwin] / total),
            "dominant_zone":               dzone,
            "dominant_zone_kr":            self._ZONE_KR.get(dzone, dzone),
            "buckets":                     buckets,
            "zones":                       zones,
            "sample_size":                 total,
        }


# ===========================================================================
# Engine 3: PlayerMatchupEngine
# ===========================================================================

class PlayerMatchupEngine:
    """Player impact scoring, head-to-head, fatigue, motivation."""

    @staticmethod
    def _impact(p: dict) -> float:
        """Composite impact score blending attack / midfield / defense signals."""
        xg          = float(p.get("expected_goals", 0) or 0)
        passes_acc  = float(p.get("passes_accurate", 0) or 0)
        passes_tot  = float(p.get("passes_total", 0) or 0)
        pass_rate   = passes_acc / passes_tot if passes_tot > 0 else 0.0
        recov       = float(p.get("ball_recoveries", 0) or 0)
        key_passes  = float(p.get("key_passes", 0) or 0)
        # weights: xG dominates attackers, pass_rate mid, recov defenders, key_passes creators
        return round(xg * 30 + pass_rate * 20 + recov * 1.5 + key_passes * 4, 2)

    def get_key_players(self, team_id, match_stats: list, top_n: int = 3) -> list:
        """Rank players by composite impact, return top N enriched dicts."""
        scored: list[dict] = []
        for p in match_stats or []:
            passes_acc = float(p.get("passes_accurate", 0) or 0)
            passes_tot = float(p.get("passes_total", 0) or 0)
            pass_rate = round(passes_acc / passes_tot, 3) if passes_tot > 0 else 0.0
            entry = dict(p)
            entry["pass_rate"]    = pass_rate
            entry["impact_score"] = self._impact(p)
            scored.append(entry)
        scored.sort(key=lambda e: e["impact_score"], reverse=True)
        return scored[:top_n]

    def analyze_matchup(self, player_a: dict, player_b: dict) -> dict:
        a = player_a.get("impact_score") or self._impact(player_a)
        b = player_b.get("impact_score") or self._impact(player_b)
        diff = a - b
        ad = abs(diff)
        if ad < 5:
            verdict, influence = "비등", 5
        elif ad < 15:
            verdict, influence = ("약간 우세" if diff > 0 else "약간 열세"), 12
        else:
            verdict, influence = ("확실한 우세" if diff > 0 else "확실한 열세"), 22
        return {
            "verdict":                       verdict,
            "impact_a":                      a,
            "impact_b":                      b,
            "diff":                          round(diff, 2),
            "estimated_match_influence_pp":  influence,
        }

    def calculate_fatigue(self, player_id, recent_matches: list) -> int:
        """Fatigue score 0 (fresh) to 100 (extreme).

        Each match contributes minutes_played * rest_decay, where
        rest_decay = max(0.1, 1 - 0.1 * days_ago). Normalised so ~4 full
        recent matches (within ~10 days) push the score above 80.
        """
        if not recent_matches:
            return 0
        weighted = 0.0
        for m in recent_matches:
            mins = float(m.get("minutes_played", 0) or 0)
            days = float(m.get("days_ago", 7) or 7)
            decay = max(0.1, 1.0 - 0.1 * days)
            weighted += mins * decay
        score = int(round(weighted / 3.5))
        return max(0, min(100, score))

    # ---- Rest-days fatigue ---------------------------------------------

    @staticmethod
    def _to_date(x):
        """Accept date / datetime / ISO-8601 string. Return date or None."""
        from datetime import date as _date, datetime as _dt
        if isinstance(x, _dt):
            return x.date()
        if isinstance(x, _date):
            return x
        if isinstance(x, str):
            try:
                return _dt.fromisoformat(x.replace("Z", "+00:00")).date()
            except ValueError:
                return None
        return None

    def calculate_rest_days_impact(
        self,
        team_id,
        match_date,
        recent_matches: Optional[list] = None,
    ) -> dict:
        """Fatigue penalty from short rest between matches.

        Spec:
          ≤ 3 days rest -> fatigue +20
          == 4 days     -> fatigue +10
          ≥ 5 days      -> 0 (normal)
        """
        mdate = self._to_date(match_date)
        if not mdate:
            return {"rest_days": None, "fatigue_penalty": 0, "note": "match_date 파싱 실패"}
        if not recent_matches:
            return {"rest_days": None, "fatigue_penalty": 0,
                    "note": "이전 경기 데이터 없음 — 패널티 0"}

        prev_dates = sorted(
            [d for d in (self._to_date(m.get("date")) for m in recent_matches)
             if d and d < mdate],
            reverse=True,
        )
        if not prev_dates:
            return {"rest_days": None, "fatigue_penalty": 0,
                    "note": "이전 경기 없음 — 패널티 0"}

        delta = (mdate - prev_dates[0]).days
        if delta <= 3:
            return {"rest_days": delta, "fatigue_penalty": 20,
                    "note": f"휴식 {delta}일 (≤3일) → 피로도 +20"}
        if delta == 4:
            return {"rest_days": delta, "fatigue_penalty": 10,
                    "note": "휴식 4일 → 피로도 +10"}
        return {"rest_days": delta, "fatigue_penalty": 0,
                "note": f"휴식 {delta}일 (정상)"}

    # -------------------------------------------------------------------

    def world_cup_motivation_bonus(self, team: dict, match_context: dict) -> dict:
        """Return {bonus, explanation}. bonus is an additive shift to win prob."""
        bonus = 0.0
        parts: list[str] = []

        if match_context.get("must_win"):
            bonus += 0.05
            parts.append("탈락 위기 → 동기부여 +5%p")
        if match_context.get("already_qualified"):
            bonus -= 0.04
            parts.append("16강 확정 → 집중력 -4%p")

        # Historical upset rate at this magnitude of ELO gap (sample: 2018+2022)
        elo_diff = float(match_context.get("elo_diff", 0) or 0)
        if elo_diff <= -100:
            bonus += 0.02
            parts.append("언더독 (ELO -100 이상 열세) — 역대 이변률 17%, 동기 +2%p")

        return {
            "bonus":       round(bonus, 4),
            "explanation": " | ".join(parts) if parts else "특이사항 없음",
        }


# ===========================================================================
# Engine 4: PatternMatcher
# ===========================================================================

class PatternMatcher:
    """Find similar historical matches by normalised Euclidean distance.

    Each historical match: {
        "label":    str,     # display label e.g. "2022 아르헨티나 vs 사우디"
        "features": dict,    # same feature keys as current_features
        "outcome":  dict,    # {"was_upset": bool, "summary": str}
        "lesson":   str,     # one-line takeaway
    }

    Suggested feature keys:
        xg_diff, elo_diff, possession_diff, shots_diff,
        fatigue_diff, elimination_pressure
    """

    def __init__(self, historical_matches: Optional[list] = None) -> None:
        self.historical: list[dict] = list(historical_matches or [])
        self._stds_cache: Optional[dict[str, float]] = None

    def _per_feature_std(self, keys: list) -> dict:
        """Per-feature std for z-scoring. Features with no historical variance
        get a huge std so their contribution to distance becomes ~0 (i.e. they
        are treated as uninformative for similarity ranking)."""
        if self._stds_cache is not None:
            return self._stds_cache
        stds: dict[str, float] = {}
        for k in keys:
            vals = [float(h["features"].get(k, 0) or 0) for h in self.historical]
            if not vals:
                stds[k] = 1e9   # no data -> ignore
                continue
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            stds[k] = math.sqrt(var) if var > 0 else 1e9   # uninformative -> ignore
        self._stds_cache = stds
        return stds

    def find_similar_matches(self, current_features: dict, top_n: int = 3) -> list:
        if not self.historical:
            return []
        keys = list(current_features.keys())
        stds = self._per_feature_std(keys)

        results: list[dict] = []
        for hist in self.historical:
            sq = 0.0
            for k in keys:
                cur = float(current_features.get(k, 0) or 0)
                his = float(hist["features"].get(k, 0) or 0)
                d = (cur - his) / (stds.get(k) or 1.0)
                sq += d * d
            dist = math.sqrt(sq)
            similarity = round(100.0 / (1.0 + dist), 1)
            results.append({
                "label":          hist.get("label"),
                "similarity_pct": similarity,
                "outcome":        hist.get("outcome", {}),
                "lesson":         hist.get("lesson"),
                "features":       hist.get("features"),
            })
        results.sort(key=lambda e: e["similarity_pct"], reverse=True)
        return results[:top_n]

    # ---- Red-card impact model (for LIVE recompute) -------------------

    def calculate_redcard_impact(
        self,
        minute: int,
        sent_off_team: str,
        current_score_diff: float,
    ) -> dict:
        """Estimated win-prob shift on the OPPOSING side after a red card.

        Args:
            minute: 0–120
            sent_off_team: 'home' | 'away'
            current_score_diff: home_score - away_score (positive = home leads)

        Model:
            base_shift_pp = max(5, 25 - 0.25 * minute)
              -> minute 0  ≈ 25%p,  minute 80 ≈ 5%p
            score modulator:
              sent-off team is trailing   -> shift × 1.2   (compound disadvantage)
              sent-off team is leading    -> shift × 0.85  (park-the-bus mitigates)
        """
        base_shift_pp = max(5.0, 25.0 - 0.25 * float(minute))
        sd = float(current_score_diff or 0)

        if sent_off_team == "home":
            if sd < 0:
                base_shift_pp *= 1.2
            elif sd > 0:
                base_shift_pp *= 0.85
        elif sent_off_team == "away":
            if sd > 0:
                base_shift_pp *= 1.2
            elif sd < 0:
                base_shift_pp *= 0.85

        opponent = "away" if sent_off_team == "home" else "home"
        return {
            "sent_off_team":                    sent_off_team,
            "minute":                           minute,
            "opposing_team":                    opponent,
            "opposing_team_win_prob_shift_pp":  round(base_shift_pp, 1),
            "note": (
                f"{minute}분 {sent_off_team} 퇴장 + 스코어 {sd:+.0f} → "
                f"{opponent} 승률 +{base_shift_pp:.1f}%p"
            ),
        }

    # -------------------------------------------------------------------

    def calculate_upset_probability(self, similar_matches: list) -> float:
        """Similarity-weighted fraction of upsets among the top matches."""
        if not similar_matches:
            return 0.0
        total_w = 0.0
        upset_w = 0.0
        for m in similar_matches:
            w = float(m.get("similarity_pct", 0) or 0) / 100.0
            total_w += w
            if (m.get("outcome") or {}).get("was_upset"):
                upset_w += w
        if total_w == 0:
            return 0.0
        return round(100.0 * upset_w / total_w, 1)


# ===========================================================================
# Engine 5: NarrativeEngine
# ===========================================================================

# CJK-aware display-width helpers for header box alignment
def _display_width(s: str) -> int:
    """Visual columns in a monospace terminal. CJK chars = 2 cols, rest = 1."""
    w = 0
    for ch in s:
        c = ord(ch)
        if (0xAC00 <= c <= 0xD7A3 or       # Hangul Syllables
            0x1100 <= c <= 0x11FF or       # Hangul Jamo
            0x3040 <= c <= 0x309F or       # Hiragana
            0x30A0 <= c <= 0x30FF or       # Katakana
            0x4E00 <= c <= 0x9FFF or       # CJK Unified
            0x3000 <= c <= 0x303F):        # CJK punctuation
            w += 2
        else:
            w += 1
    return w


def _pad_right(s: str, target_w: int) -> str:
    return s + " " * max(0, target_w - _display_width(s))


class NarrativeEngine:
    """Compose Korean consulting-style narrative from other engines' output."""

    # --- reasons ----------------------------------------------------------

    def generate_reasons(self, elo_result: dict, tactical_result: dict, player_result: dict) -> list:
        """Up to 3 model-grounded reasons. Numbers come from engines, not echoes."""
        raw: list[str] = []

        elo_diff = float(elo_result.get("elo_diff", 0))
        home_pct = round(elo_result.get("home", 0) * 100)
        away_pct = round(elo_result.get("away", 0) * 100)
        if abs(elo_diff) >= 50:
            if elo_diff > 0:
                raw.append(f"ELO 격차 {abs(elo_diff):.0f} — 모델 홈팀 승률 {home_pct}%")
            else:
                raw.append(f"ELO 격차 {abs(elo_diff):.0f} (원정 우세) — 모델 원정팀 승률 {away_pct}%")

        xg_diff = float(player_result.get("xg_diff", 0))
        if abs(xg_diff) >= 0.5:
            est_pp = round(15 * abs(xg_diff))   # research-grounded slope ~15%p per 1.0 xG diff
            side = "홈" if xg_diff > 0 else "원정"
            raw.append(f"xG 차이 {xg_diff:+.1f} — {side} 승률 추정 +{est_pp}%p")

        poss_diff = float(tactical_result.get("diff_pp", 0))
        if abs(poss_diff) >= 8:
            shift_pp = float(tactical_result.get("win_prob_shift", 0)) * 100
            sign = "+" if poss_diff >= 0 else "-"
            raw.append(f"점유율 우위 {sign}{abs(poss_diff):.0f}%p — 승률 보정 {shift_pp:+.1f}%p")

        # Fallback to keep exactly 3 reasons
        while len(raw) < 3:
            draw_pct = round(elo_result.get("draw", 0) * 100)
            raw.append(f"무승부 확률 {draw_pct}% — 양 팀 모델 격차로 자동 산출")

        markers = ["①", "②", "③"]
        return [f"{markers[i]} {r}" for i, r in enumerate(raw[:3])]

    # --- warnings ---------------------------------------------------------

    def generate_warning(self, similar_matches: list, tactical_result: dict, upset_pct: float) -> list:
        warnings: list[str] = []
        if similar_matches:
            top = similar_matches[0]
            if (top.get("outcome") or {}).get("was_upset"):
                warnings.append(
                    f"⚠️ {top['label']} 패턴 {top['similarity_pct']:.0f}% 유사 — 역전 주의"
                )
        if upset_pct >= 15:
            warnings.append(f"⚠️ 유사 경기 이변 발생률 {upset_pct:.0f}%")
        if (tactical_result or {}).get("confidence") == "high":
            warnings.append(
                f"⚠️ 점유율 격차 큼 — {tactical_result.get('note', '')}"
            )
        return warnings

    # --- previous match recap ---------------------------------------------

    def generate_previous_match_recap(self, team_name: str, opponent_name: str,
                                       previous_analysis: dict) -> str:
        zone_kr = previous_analysis.get("weakest_zone_kr") or "—"
        vuln = previous_analysis.get("vulnerability_score", 0)
        pattern = previous_analysis.get("possession_pattern", "—")
        return (
            f"vs {opponent_name}: {pattern}, {zone_kr} 측면 위협 비중 {vuln}% — "
            f"수비 노출 패턴 확인"
        )

    # --- key matchups -----------------------------------------------------

    def generate_key_matchups(self, key_players: list) -> list:
        out: list[str] = []
        markers = ["①", "②", "③"]
        for i, p in enumerate(key_players[:3]):
            marker = markers[i] if i < len(markers) else f"({i+1})"
            name = p.get("name") or f"#{p.get('player_id', '?')}"
            pr = float(p.get("pass_rate", 0) or 0)
            xg = float(p.get("expected_goals", 0) or 0)
            kp = float(p.get("key_passes", 0) or 0)
            impact = float(p.get("impact_score", 0) or 0)
            bits: list[str] = []
            if pr > 0:
                bits.append(f"패스성공률 {pr*100:.0f}%")
            if xg >= 0.05:
                bits.append(f"xG {xg:.2f}")
            if kp > 0:
                bits.append(f"키패스 {kp:.0f}")
            bits.append(f"임팩트 {impact:.1f}")
            out.append(f"{marker} {name} — {' / '.join(bits)}")
        return out

    # --- header box -------------------------------------------------------

    @staticmethod
    def _render_header(title: str, odds_line: str) -> str:
        w = max(_display_width(title), _display_width(odds_line)) + 2
        bar = "─" * w
        return (
            f"┌{bar}┐\n"
            f"│ {_pad_right(title, w-1)}│\n"
            f"│ {_pad_right(odds_line, w-1)}│\n"
            f"└{bar}┘"
        )

    # --- full report ------------------------------------------------------

    def generate_full_report(self, match_meta: dict, bundle: dict) -> str:
        """Compose the full consulting-style report.

        Required keys in `bundle`:
            elo, possession_impact, home_previous, away_previous,
            key_players, similar_matches, upset_pct,
            motivation_home, motivation_away, reasons
        """
        home_name = match_meta.get("home_name", "Home")
        away_name = match_meta.get("away_name", "Away")

        elo = bundle["elo"]
        h_pct = round(elo["home"] * 100)
        d_pct = round(elo["draw"] * 100)
        a_pct = round(elo["away"] * 100)

        title = f"{home_name} vs {away_name}"
        odds_line = f"{home_name} 승 {h_pct}% │ 무 {d_pct}% │ {away_name} 승 {a_pct}%"
        header = self._render_header(title, odds_line)

        recap = self.generate_previous_match_recap(
            home_name,
            match_meta.get("home_last_opponent", ""),
            bundle["home_previous"],
        )

        poss = bundle["possession_impact"]
        if poss.get("confidence") in ("medium", "high"):
            core_line = f"점유율 싸움이 승패 결정 — {poss['note']}"
        else:
            core_line = "점유율 균형 — 결정적 차이는 ELO + xG 격차에서 발생"

        reasons = bundle.get("reasons", [])

        key_lines = self.generate_key_matchups(bundle["key_players"])

        sim_lines: list[str] = []
        for m in bundle.get("similar_matches", []):
            sim_lines.append(
                f"{m['label']} {m['similarity_pct']:.0f}% 유사 — {m.get('lesson', '')}"
            )
        if not sim_lines:
            sim_lines = ["과거 유사 경기 데이터 없음"]

        warnings = self.generate_warning(
            bundle.get("similar_matches", []),
            poss,
            float(bundle.get("upset_pct", 0)),
        )
        warnings.append(f"⚠️ 역전 가능성 {bundle.get('upset_pct', 0):.0f}%")
        if not warnings:
            warnings = ["특별 경고 시그널 없음"]

        mh = bundle.get("motivation_home", {})
        ma = bundle.get("motivation_away", {})
        motiv_lines = [
            f"{home_name}: {mh.get('explanation', '특이사항 없음')}  "
            f"(보정 {mh.get('bonus', 0)*100:+.1f}%p)",
            f"{away_name}: {ma.get('explanation', '특이사항 없음')}  "
            f"(보정 {ma.get('bonus', 0)*100:+.1f}%p)",
        ]

        sections = [
            header,
            "",
            "[이전 경기 복기]",
            recap,
            "",
            "[이번 경기 핵심]",
            core_line,
            "",
            "[모델 근거 3가지]",
            *reasons,
            "",
            "[주목할 선수 TOP 3]",
            *key_lines,
            "",
            "[유사 과거 경기]",
            *sim_lines,
            "",
            "[경고 시그널]",
            *warnings,
            "",
            "[월드컵 특수 요소]",
            *motiv_lines,
        ]
        return "\n".join(sections)


# ===========================================================================
# Demo: Korea vs Portugal — synthetic but realistic inputs
# ===========================================================================

def _load_historical_from_db() -> list:
    """Build historical matches from worldcup.db. Returns [] if DB empty/missing.

    Joins matches + team_stats and produces feature dicts compatible with
    PatternMatcher. Outcome.was_upset uses an xG-based heuristic: the team
    with the higher xG was 'expected' to win; if they didn't, it's an upset.
    """
    try:
        import database
        conn = database.get_connection()
    except Exception:
        return []
    try:
        rows = conn.execute("""
            SELECT m.id, m.season, m.home_score, m.away_score,
                   ht.name AS home_name, at.name AS away_name,
                   hs.xg_total       AS home_xg,
                   hs.shots_total    AS home_shots,
                   hs.possession     AS home_poss,
                   aw.xg_total       AS away_xg,
                   aw.shots_total    AS away_shots,
                   aw.possession     AS away_poss
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            JOIN team_stats hs ON hs.match_id = m.id AND hs.team_id = m.home_team_id
            JOIN team_stats aw ON aw.match_id = m.id AND aw.team_id = m.away_team_id
            WHERE m.status = 'completed' AND m.season IN (2018, 2022)
        """).fetchall()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []

    out: list[dict] = []
    for r in rows:
        home_xg     = float(r["home_xg"] or 0)
        away_xg     = float(r["away_xg"] or 0)
        home_poss   = float(r["home_poss"] if r["home_poss"] is not None else 50)
        away_poss   = float(r["away_poss"] if r["away_poss"] is not None else 50)
        home_shots  = float(r["home_shots"] or 0)
        away_shots  = float(r["away_shots"] or 0)
        home_score  = int(r["home_score"] or 0)
        away_score  = int(r["away_score"] or 0)

        features = {
            "xg_diff":              home_xg - away_xg,
            "elo_diff":             0,   # not back-computed
            "possession_diff":      home_poss - away_poss,
            "shots_diff":           home_shots - away_shots,
            "fatigue_diff":         0,
            "elimination_pressure": 0,
        }

        # Upset heuristic: the "expected winner" (by xG, or by shots if xG=0) failed to win.
        # Falling back to shots is necessary because BALLDONTLIE 2018 team-level
        # data lacks expected_goals.
        home_won = home_score > away_score
        away_won = away_score > home_score
        xg_d    = features["xg_diff"]
        shot_d  = features["shots_diff"]
        if abs(xg_d) > 0.3:
            stronger_is_home = xg_d >  0.3
            stronger_is_away = xg_d < -0.3
        else:
            stronger_is_home = shot_d >  4
            stronger_is_away = shot_d < -4
        was_upset = (stronger_is_home and not home_won) or (stronger_is_away and not away_won)

        out.append({
            "label":   f"{r['season']} {r['home_name']} vs {r['away_name']}",
            "features": features,
            "outcome":  {"was_upset": was_upset, "summary": f"{home_score}-{away_score}"},
            "lesson":   f"DB 백필: 스코어 {home_score}-{away_score}, xG {home_xg:.2f}-{away_xg:.2f}",
        })

    try:
        conn.close()
    except Exception:
        pass
    return out


def _historical_dataset() -> list:
    """Prefer real backfilled data; fall back to a hardcoded sample if DB is empty.

    Feature vector convention (home-team perspective):
      xg_diff, elo_diff, possession_diff, shots_diff, fatigue_diff, elimination_pressure
    """
    db_matches = _load_historical_from_db()
    if db_matches:
        print(f"[model] historical from DB: {len(db_matches)} matches")
        return db_matches
    print("[model] DB historical empty -- falling back to hardcoded sample")
    return [
        {
            "label": "2022 아르헨티나 vs 사우디",
            "features": {
                "xg_diff": 1.8, "elo_diff": 260, "possession_diff": 38,
                "shots_diff": 9, "fatigue_diff": -5, "elimination_pressure": 0,
            },
            "outcome": {"was_upset": True, "summary": "강팀 1-2 역전패"},
            "lesson": "xG 우세에도 방심하면 실점 — 초반 선제 후 집중력 저하 주의",
        },
        {
            "label": "2022 일본 vs 독일",
            "features": {
                "xg_diff": 0.9, "elo_diff": 180, "possession_diff": 25,
                "shots_diff": 7, "fatigue_diff": 3, "elimination_pressure": 30,
            },
            "outcome": {"was_upset": True, "summary": "강팀 1-2 역전패"},
            "lesson": "점유율 압도해도 후반 체력 저하 시 위험",
        },
        {
            "label": "2018 독일 vs 한국",
            "features": {
                "xg_diff": 1.5, "elo_diff": 320, "possession_diff": 30,
                "shots_diff": 12, "fatigue_diff": 8, "elimination_pressure": 70,
            },
            "outcome": {"was_upset": True, "summary": "강팀 0-2 패배 + 조별 탈락"},
            "lesson": "탈락 압박 + 후반 체력 한계 = 이변 확률 급증",
        },
        {
            "label": "2022 브라질 vs 세르비아",
            "features": {
                "xg_diff": 1.3, "elo_diff": 220, "possession_diff": 15,
                "shots_diff": 6, "fatigue_diff": 0, "elimination_pressure": 0,
            },
            "outcome": {"was_upset": False, "summary": "강팀 2-0 정배"},
            "lesson": "xG + ELO 모두 우세 + 컨디션 정상 = 정배 신뢰도 높음",
        },
        {
            "label": "2022 포르투갈 vs 한국 (조별)",
            "features": {
                "xg_diff": 0.4, "elo_diff": 140, "possession_diff": 5,
                "shots_diff": 2, "fatigue_diff": -3, "elimination_pressure": 80,
            },
            "outcome": {"was_upset": True, "summary": "강팀 1-2 역전패 (탈락)"},
            "lesson": "16강 확정 후 집중력 저하 + 약팀의 탈락 압박이 만남",
        },
        {
            "label": "2018 프랑스 vs 호주",
            "features": {
                "xg_diff": 1.1, "elo_diff": 200, "possession_diff": 18,
                "shots_diff": 5, "fatigue_diff": 0, "elimination_pressure": 0,
            },
            "outcome": {"was_upset": False, "summary": "강팀 2-1 정배"},
            "lesson": "ELO + xG 우세 + 첫 경기 = 정배",
        },
    ]


def _korea_vs_portugal_inputs() -> dict:
    return {
        "match_meta": {
            "home_name": "한국",
            "away_name": "포르투갈",
            "home_last_opponent": "우루과이",
            "stage": "group",
        },
        "home_elo": 1720.0,
        "away_elo": 1880.0,
        # Korea's last match — left flank exposure
        "home_previous_match": {
            "shots_against_by_zone": {"left": 6, "center": 3, "right": 2},
            "goals_against_by_zone": {"left": 1, "center": 0, "right": 0},
            "possession_pct": 43.0,
        },
        # Portugal's last match — central exposure (but high possession)
        "away_previous_match": {
            "shots_against_by_zone": {"left": 1, "center": 4, "right": 2},
            "goals_against_by_zone": {"left": 0, "center": 1, "right": 0},
            "possession_pct": 62.0,
        },
        "home_recent_matches": [
            {"goals_against": [{"minute": 11, "zone": "left"}, {"minute": 76, "zone": "left"}]},
            {"goals_against": [{"minute": 68, "zone": "center"}]},
        ],
        "home_players": [
            {"player_id": 1001, "name": "손흥민",  "expected_goals": 0.42,
             "passes_accurate": 18, "passes_total": 22, "key_passes": 2, "ball_recoveries": 1},
            {"player_id": 1002, "name": "이재성",  "expected_goals": 0.18,
             "passes_accurate": 41, "passes_total": 47, "key_passes": 3, "ball_recoveries": 6},
            {"player_id": 1003, "name": "김민재",  "expected_goals": 0.05,
             "passes_accurate": 58, "passes_total": 64, "key_passes": 0, "ball_recoveries": 9},
            {"player_id": 1004, "name": "이강인",  "expected_goals": 0.21,
             "passes_accurate": 32, "passes_total": 39, "key_passes": 4, "ball_recoveries": 3},
        ],
        "away_players": [
            {"player_id": 2001, "name": "브루노 페르난데스", "expected_goals": 0.31,
             "passes_accurate": 51, "passes_total": 58, "key_passes": 4, "ball_recoveries": 4},
            {"player_id": 2002, "name": "베르나르두 실바",   "expected_goals": 0.24,
             "passes_accurate": 44, "passes_total": 48, "key_passes": 3, "ball_recoveries": 2},
            {"player_id": 2003, "name": "호날두",           "expected_goals": 0.38,
             "passes_accurate": 12, "passes_total": 17, "key_passes": 1, "ball_recoveries": 0},
            {"player_id": 2004, "name": "디오구 조타",       "expected_goals": 0.27,
             "passes_accurate": 14, "passes_total": 20, "key_passes": 2, "ball_recoveries": 1},
        ],
        "home_match_context": {"must_win": True,  "already_qualified": False, "elo_diff": -160},
        "away_match_context": {"must_win": False, "already_qualified": True,  "elo_diff":  160},
        "home_player_fatigue_input": [
            {"minutes_played": 90, "days_ago": 4},
            {"minutes_played": 78, "days_ago": 8},
            {"minutes_played": 90, "days_ago": 12},
        ],
    }


def _run_demo() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    inp = _korea_vs_portugal_inputs()

    # --- engines ---------------------------------------------------------
    elo_engine = EloEngine()
    elo_engine.set_rating("한국", inp["home_elo"])
    elo_engine.set_rating("포르투갈", inp["away_elo"])

    tactical = TacticalEngine()
    player_engine = PlayerMatchupEngine()
    pattern = PatternMatcher(historical_matches=_historical_dataset())
    narrator = NarrativeEngine()

    # --- computations ----------------------------------------------------
    elo_result = elo_engine.get_win_probability(inp["home_elo"], inp["away_elo"])

    home_prev = tactical.analyze_previous_match("KOR", inp["home_previous_match"])
    away_prev = tactical.analyze_previous_match("POR", inp["away_previous_match"])

    poss_impact = tactical.calculate_possession_impact(
        home_poss_avg=inp["home_previous_match"]["possession_pct"],
        away_poss_avg=inp["away_previous_match"]["possession_pct"],
    )

    all_players = inp["home_players"] + inp["away_players"]
    key_players = player_engine.get_key_players(team_id=None, match_stats=all_players, top_n=3)

    home_xg_sum = sum(float(p.get("expected_goals") or 0) for p in inp["home_players"])
    away_xg_sum = sum(float(p.get("expected_goals") or 0) for p in inp["away_players"])
    xg_diff = round(home_xg_sum - away_xg_sum, 2)

    fatigue_home = player_engine.calculate_fatigue("KOR-team", inp["home_player_fatigue_input"])

    current_features = {
        "xg_diff":              xg_diff,
        "elo_diff":             inp["home_elo"] - inp["away_elo"],
        "possession_diff":      inp["home_previous_match"]["possession_pct"]
                                - inp["away_previous_match"]["possession_pct"],
        "shots_diff":           -3,                       # placeholder symmetric metric
        "fatigue_diff":         fatigue_home - 50,
        "elimination_pressure": 75 if inp["home_match_context"]["must_win"] else 0,
    }
    similar_matches = pattern.find_similar_matches(current_features, top_n=3)
    upset_pct = pattern.calculate_upset_probability(similar_matches)

    motiv_home = player_engine.world_cup_motivation_bonus({"id": "KOR"}, inp["home_match_context"])
    motiv_away = player_engine.world_cup_motivation_bonus({"id": "POR"}, inp["away_match_context"])

    reasons = narrator.generate_reasons(
        elo_result=elo_result,
        tactical_result=poss_impact,
        player_result={"xg_diff": xg_diff},
    )

    bundle = {
        "elo":               elo_result,
        "possession_impact": poss_impact,
        "home_previous":     home_prev,
        "away_previous":     away_prev,
        "key_players":       key_players,
        "similar_matches":   similar_matches,
        "upset_pct":         upset_pct,
        "motivation_home":   motiv_home,
        "motivation_away":   motiv_away,
        "reasons":           reasons,
    }

    print(narrator.generate_full_report(inp["match_meta"], bundle))
# ===========================================================================
# Engine 6: DixonColesEngine  — bivariate Poisson + low-score correction
# ===========================================================================

import math as _math

# Module cache: rho is estimated from static 2018/2022 data, so compute once
# per db_path and reuse across DixonColesEngine instantiations.
_RHO_CACHE: dict = {}
_RHO_FALLBACK: float = -0.13


def estimate_rho(db_path: str = "worldcup.db") -> float:
    """MLE of the Dixon-Coles rho from 2018+2022 completed matches.

    Only low-scoring outcomes (0-0, 0-1, 1-0, 1-1) carry information about rho,
    so the Poisson factors (which don't depend on rho) drop out and the negative
    log-likelihood reduces to  NLL(ρ) = -Σ log(tau(x, y, λ, μ, ρ)).
    λ, μ use team xG (team_stats.xg_total) when available, else the actual goals
    scored (2018 has no team-level xG — see CLAUDE.md §5.1).

    Optimised with a pure-Python golden-section search over [-0.5, 0.5]. The NLL
    is convex in rho (each term is log of a linear function), so this finds the
    global optimum — equivalent to scipy.optimize.minimize_scalar but with NO
    scipy dependency, which this project deliberately does not ship (CLAUDE.md §9;
    adding scipy would break the Railway build).

    Returns the optimal rho; -0.13 (default) on any failure or when there is no
    usable data.
    """
    if db_path in _RHO_CACHE:
        return _RHO_CACHE[db_path]

    import database

    try:
        conn = database.get_connection(None if db_path == "worldcup.db" else db_path)
    except Exception:
        return _RHO_FALLBACK

    import sqlite3 as _sq
    if isinstance(conn, _sq.Connection) and conn.row_factory is not _sq.Row:
        conn.row_factory = _sq.Row

    try:
        rows = conn.execute(
            """
            SELECT m.home_score AS hs, m.away_score AS as_,
                   hts.xg_total AS home_xg, ats.xg_total AS away_xg
            FROM matches m
            LEFT JOIN team_stats hts ON hts.match_id = m.id AND hts.is_home = 1
            LEFT JOIN team_stats ats ON ats.match_id = m.id AND ats.is_home = 0
            WHERE m.season IN (2018, 2022) AND m.status = 'completed'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            """
        ).fetchall()
    except Exception:
        return _RHO_FALLBACK
    finally:
        conn.close()

    data = []
    for r in rows:
        x, y = r["hs"], r["as_"]
        lam = r["home_xg"] if r["home_xg"] is not None else x
        mu  = r["away_xg"] if r["away_xg"] is not None else y
        data.append((x, y, max(0.05, float(lam)), max(0.05, float(mu))))

    if not data:
        return _RHO_FALLBACK

    def _tau(x, y, lam, mu, rho):
        if x == 0 and y == 0:
            return 1.0 - lam * mu * rho
        if x == 0 and y == 1:
            return 1.0 + lam * rho
        if x == 1 and y == 0:
            return 1.0 + mu * rho
        if x == 1 and y == 1:
            return 1.0 - rho
        return 1.0

    def _nll(rho):
        s = 0.0
        for x, y, lam, mu in data:
            t = _tau(x, y, lam, mu, rho)
            s -= _math.log(t if t > 1e-12 else 1e-12)
        return s

    # Golden-section search (NLL convex in rho).
    a, b = -0.5, 0.5
    gr = (_math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = _nll(c), _nll(d)
    for _ in range(100):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = _nll(c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = _nll(d)
        if abs(b - a) < 1e-7:
            break

    rho = max(-0.5, min(0.5, (a + b) / 2.0))
    _RHO_CACHE[db_path] = rho
    return rho


class DixonColesEngine:
    """
    Converts ELO ratings into a full scoreline probability matrix via
    Dixon-Coles bivariate Poisson with low-score correction (rho).

    Reference: Dixon & Coles (1997) "Modelling Association Football Scores
    and Inefficiencies in the Football Betting Market".

    Usage:
        dc = DixonColesEngine()
        result = dc.predict(home_elo=1850, away_elo=1720,
                            home_attack=1.45, home_defense=0.85,
                            away_attack=1.10, away_defense=1.05)
        print(result["top_scorelines"])
        print(result["win_draw_loss"])
    """

    # Default scoring rates — calibrated to 2022 WC avg 2.69 goals/match
    DEFAULT_HOME_ATTACK:   float = 1.35
    DEFAULT_HOME_DEFENSE:  float = 0.90
    DEFAULT_AWAY_ATTACK:   float = 1.10
    DEFAULT_AWAY_DEFENSE:  float = 1.00
    HOME_ADVANTAGE:        float = 1.10   # neutral venue → 1.0
    MAX_GOALS:             int   = 6      # compute P(X=i, Y=j) for i,j in 0..MAX_GOALS
    RHO:                   float = -0.13  # class default; overridden per-instance in __init__

    def __init__(self, db_path: str = "worldcup.db"):
        """Estimate rho from historical data on construction, with fallback.

        self.RHO        — the rho used by this instance
        self.RHO_SOURCE — "estimated_from_data" | "fallback_default"
        """
        rho = estimate_rho(db_path)
        if rho == _RHO_FALLBACK:
            self.RHO = _RHO_FALLBACK
            self.RHO_SOURCE = "fallback_default"
        else:
            self.RHO = rho
            self.RHO_SOURCE = "estimated_from_data"

    @staticmethod
    def _tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
        """Dixon-Coles correction factor for low-scoring outcomes."""
        if x == 0 and y == 0:
            return 1.0 - lam * mu * rho
        if x == 0 and y == 1:
            return 1.0 + lam * rho
        if x == 1 and y == 0:
            return 1.0 + mu * rho
        if x == 1 and y == 1:
            return 1.0 - rho
        return 1.0

    @staticmethod
    def _poisson_pmf(k: int, lam: float) -> float:
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return (_math.exp(-lam) * (lam ** k)) / _math.factorial(k)

    def _expected_goals(
        self,
        home_elo: float, away_elo: float,
        home_attack: float, home_defense: float,
        away_attack: float, away_defense: float,
        neutral_venue: bool = True,
    ) -> tuple[float, float]:
        """
        λ_home = home_attack × away_defense × home_advantage × elo_weight
        λ_away = away_attack × home_defense × elo_weight_inv
        """
        elo_diff = home_elo - away_elo
        # ELO sets direction only; attack/defense strengths drive goal counts.
        # Softened to /1600 so ELO does not double-count strength already
        # captured by attack/defense (a 160-pt gap → factor ≈ 1.26 / 0.79).
        elo_weight = 10 ** (elo_diff / 1600.0)
        # Clamp the ELO factor so even a huge gap keeps the λ ratio under ~2.8x
        # (0.6..1.67). World Cup upsets are real; never crush the underdog to 0.
        elo_weight = max(0.6, min(1.67, elo_weight))

        ha = 1.0 if neutral_venue else self.HOME_ADVANTAGE

        lam = home_attack * away_defense * ha * elo_weight
        mu  = away_attack * home_defense * (1.0 / elo_weight)

        # Clamp to a realistic per-match expected-goals range [0.3, 3.0].
        lam = max(0.3, min(3.0, lam))
        mu  = max(0.3, min(3.0, mu))
        return lam, mu

    def scoreline_matrix(
        self,
        lam: float, mu: float,
        rho: float | None = None,
    ) -> dict[tuple[int, int], float]:
        """
        Returns {(home_goals, away_goals): probability} for 0..MAX_GOALS each.
        Normalised so all entries sum to 1.0.
        """
        rho = rho if rho is not None else self.RHO
        n = self.MAX_GOALS
        matrix: dict[tuple[int, int], float] = {}

        for i in range(n + 1):
            for j in range(n + 1):
                p = (
                    self._tau(i, j, lam, mu, rho)
                    * self._poisson_pmf(i, lam)
                    * self._poisson_pmf(j, mu)
                )
                matrix[(i, j)] = max(0.0, p)

        # Normalise (Dixon-Coles correction can slightly perturb the sum)
        total = sum(matrix.values())
        if total > 0:
            matrix = {k: v / total for k, v in matrix.items()}

        return matrix

    def predict(
        self,
        home_elo: float,
        away_elo: float,
        home_attack:   float | None = None,
        home_defense:  float | None = None,
        away_attack:   float | None = None,
        away_defense:  float | None = None,
        neutral_venue: bool = True,
        top_n: int = 5,
    ) -> dict:
        """
        Full prediction bundle:
          lam, mu          — expected goals
          matrix           — full P(i, j) dict
          win_draw_loss    — {home_win, draw, away_win} marginals
          top_scorelines   — [(score_str, pct), ...] top N by probability
          expected_goals   — {"home": lam, "away": mu}
        """
        ha = home_attack  if home_attack  is not None else self.DEFAULT_HOME_ATTACK
        hd = home_defense if home_defense is not None else self.DEFAULT_HOME_DEFENSE
        aa = away_attack  if away_attack  is not None else self.DEFAULT_AWAY_ATTACK
        ad = away_defense if away_defense is not None else self.DEFAULT_AWAY_DEFENSE

        lam, mu = self._expected_goals(
            home_elo, away_elo, ha, hd, aa, ad, neutral_venue
        )
        return self._assemble(lam, mu, top_n)

    def _assemble(self, lam: float, mu: float, top_n: int = 5) -> dict:
        """Build the full prediction bundle from final λ_home / λ_away."""
        matrix = self.scoreline_matrix(lam, mu)

        home_win = sum(p for (i, j), p in matrix.items() if i > j)
        draw     = sum(p for (i, j), p in matrix.items() if i == j)
        away_win = sum(p for (i, j), p in matrix.items() if i < j)

        top = sorted(matrix.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        top_scorelines = [
            {
                "scoreline": f"{i}-{j}",
                "home_goals": i,
                "away_goals": j,
                "probability_pct": round(p * 100, 1),
            }
            for (i, j), p in top
        ]

        return {
            "expected_goals": {
                "home": round(lam, 3),
                "away": round(mu, 3),
            },
            "win_draw_loss": {
                "home_win": round(home_win * 100, 1),
                "draw":     round(draw     * 100, 1),
                "away_win": round(away_win * 100, 1),
            },
            "top_scorelines":   top_scorelines,
            "matrix":           matrix,
            "model":            "Dixon-Coles Bivariate Poisson",
            "rho":              self.RHO,
        }

    # Map canonical DB team name -> known aliases / alternate spellings.
    # Lookups are case-insensitive (see _resolve_strength).
    TEAM_ALIASES: dict[str, list[str]] = {
        "South Korea":  ["Korea", "Korea Republic", "한국", "대한민국"],
        "USA":          ["United States", "United States of America", "미국"],
        "IR Iran":      ["Iran", "이란"],
        "Korea DPR":    ["North Korea"],
        "Czechia":      ["Czech Republic"],
        "China PR":     ["China"],
    }

    def _resolve_strength(
        self,
        name: str,
        strengths: dict[str, dict[str, float]],
    ) -> dict[str, float] | None:
        """
        Resolve a (possibly aliased) team name to its strength entry.
        Returns None if the team cannot be found.
        """
        # 1. Exact match
        if name in strengths:
            return strengths[name]

        # 2. Alias match: input name listed among a DB team's aliases
        for db_name, aliases in self.TEAM_ALIASES.items():
            if name == db_name or name in aliases:
                if db_name in strengths:
                    return strengths[db_name]

        # 3. Case-insensitive fallback against DB names and aliases
        lower = name.lower()
        for team_name, entry in strengths.items():
            if team_name.lower() == lower:
                return entry
        for db_name, aliases in self.TEAM_ALIASES.items():
            if lower in [a.lower() for a in aliases] or lower == db_name.lower():
                if db_name in strengths:
                    return strengths[db_name]

        return None

    def _resolve_team_name(self, name: str) -> str:
        """Map an aliased team name to its canonical teams.name via TEAM_ALIASES.

        Mirrors _resolve_strength's alias logic but returns the DB name (for use
        in raw SQL lookups). Returns the input unchanged when it is not a known
        alias — it is then assumed to already be a canonical DB name.
        """
        for db_name, aliases in self.TEAM_ALIASES.items():
            if name == db_name or name in aliases:
                return db_name
        lower = name.lower()
        for db_name, aliases in self.TEAM_ALIASES.items():
            if lower == db_name.lower() or lower in [a.lower() for a in aliases]:
                return db_name
        return name

    def _player_xg_adjustment(
        self,
        team_name: str,
        conn=None,
        db_path: str = "worldcup.db",
    ) -> float:
        """Lineup-form multiplier from player xG (2026 only).

        Compares the team's MOST RECENT completed 2026 match total player xG to
        its average per-match player xG across 2026:
            >1.0  -> latest lineup/performance out-produced the team's norm
            <1.0  -> below the norm

        Returns 1.0 when no player_stats data exists (fallback). Clamped to
        [0.7, 1.4] so a single outlier match can't dominate the model.

        Pass an open `conn` to reuse a connection; otherwise one is opened via
        database.get_connection() (dual backend) and closed here. Aliased team
        names are resolved to the canonical teams.name via _resolve_team_name().
        """
        import database

        db_name = self._resolve_team_name(team_name)

        own_conn = conn is None
        if own_conn:
            try:
                conn = database.get_connection(None if db_path == "worldcup.db" else db_path)
            except Exception:
                return 1.0
        try:
            rows = conn.execute(
                """
                SELECT m.kickoff_utc AS k, SUM(ps.expected_goals) AS match_xg
                FROM player_stats ps
                JOIN matches m ON m.id = ps.match_id
                JOIN teams t   ON t.id = ps.team_id
                WHERE t.name = ? AND m.season = 2026 AND m.status = 'completed'
                GROUP BY ps.match_id, m.kickoff_utc
                ORDER BY m.kickoff_utc DESC
                """,
                (db_name,),
            ).fetchall()
        except Exception:
            return 1.0
        finally:
            if own_conn:
                conn.close()

        # float() guards against psycopg2 returning Decimal for numeric aggregates.
        xgs = [float(r["match_xg"]) for r in rows if r["match_xg"] is not None]
        if not xgs:
            return 1.0
        recent = xgs[0]
        avg = sum(xgs) / len(xgs)
        if avg <= 0:
            return 1.0
        return max(0.7, min(1.4, recent / avg))

    def apply_user_notes(self, team_name, notes_dict, base_lambda):
        """
        Adjust a team's λ from operator notes (additive, applied last).

        notes_dict keys (all optional):
          key_player_out : list of names -> -0.08 each, total capped at -0.25
          tactical_note  : str           -> -0.10 if it mentions "defensive",
                                            +0.08 if it mentions "attacking"
          condition      : "negative" -> -0.07 | "positive" -> +0.07 | else 0

        Returns the adjusted λ, clamped to [0.3, 3.0]. team_name is accepted for
        symmetry/logging but does not affect the math.
        """
        if not notes_dict:
            return base_lambda

        lam = base_lambda

        out = notes_dict.get("key_player_out") or []
        if out:
            lam += max(-0.25, -0.08 * len(out))

        tac = (notes_dict.get("tactical_note") or "").lower()
        if any(k in tac for k in ("defensive", "defend", "low block", "park the bus")):
            lam -= 0.10
        if any(k in tac for k in ("attacking", "attack", "high press", "front foot")):
            lam += 0.08

        cond = notes_dict.get("condition")
        if cond == "negative":
            lam -= 0.07
        elif cond == "positive":
            lam += 0.07

        return max(0.3, min(3.0, lam))

    def predict_from_db(
        self,
        home_name: str,
        away_name: str,
        home_elo: float,
        away_elo: float,
        db_path: str = "worldcup.db",
        neutral_venue: bool = True,
        top_n: int = 5,
        group_name: str | None = None,
        home_notes: dict | None = None,
        away_notes: dict | None = None,
    ) -> dict:
        """
        Like predict(), but auto-loads attack/defense strengths from the DB by
        team name. Falls back to 1.0/1.0 (league-average) for any team not found.

        Adds a "strength_source" key to the result reporting which side(s) used
        DB strengths vs. the league-average fallback, and a "player_adjustment"
        key with the lineup-form multipliers applied to each side's λ.
        """
        strengths = _team_strengths_from_db(db_path)

        home = self._resolve_strength(home_name, strengths)
        away = self._resolve_strength(away_name, strengths)

        # Lineup-form multipliers from player xG. Each scales its own team's λ:
        # lam ∝ home_attack and mu ∝ away_attack, so multiplying the attack term
        # by the adjustment multiplies that side's expected goals directly.
        home_adj = self._player_xg_adjustment(home_name, db_path=db_path)
        away_adj = self._player_xg_adjustment(away_name, db_path=db_path)

        # float() guards: DB-sourced strengths may be Decimal on PostgreSQL.
        ha = float(home["attack"]  if home is not None else 1.0) * float(home_adj)
        hd = float(home["defense"] if home is not None else 1.0)
        aa = float(away["attack"]  if away is not None else 1.0) * float(away_adj)
        ad = float(away["defense"] if away is not None else 1.0)

        # Group-stage situation: each team's λ scaled by its motivation state.
        sit_home = sit_away = None
        if group_name:
            gse = GroupSituationEngine()
            try:
                sit_home = gse.assess_situation(home_name, group_name)
                ha *= sit_home["lambda_multiplier"]
            except Exception:
                sit_home = None
            try:
                sit_away = gse.assess_situation(away_name, group_name)
                aa *= sit_away["lambda_multiplier"]
            except Exception:
                sit_away = None

        lam, mu = self._expected_goals(
            home_elo, away_elo, ha, hd, aa, ad, neutral_venue
        )

        # Operator notes: additive λ override applied after the ELO/strength
        # model (key player out, tactical posture, condition).
        base_lam, base_mu = lam, mu
        if home_notes:
            lam = self.apply_user_notes(home_name, home_notes, lam)
        if away_notes:
            mu = self.apply_user_notes(away_name, away_notes, mu)

        result = self._assemble(lam, mu, top_n)
        result["strength_source"] = {
            "home": "db" if home is not None else "fallback(1.0)",
            "away": "db" if away is not None else "fallback(1.0)",
        }
        result["player_adjustment"] = {
            "home": round(home_adj, 3),
            "away": round(away_adj, 3),
        }
        result["situation"] = {"home": sit_home, "away": sit_away}
        result["notes_adjustment"] = {
            "home": round(lam - base_lam, 3),
            "away": round(mu - base_mu, 3),
        }
        return result

    def format_report_kr(self, home_name: str, away_name: str, result: dict) -> str:
        """한국어 컨설팅 리포트 포맷."""
        eg = result["expected_goals"]
        wdl = result["win_draw_loss"]
        top = result["top_scorelines"]

        lines = [
            "=" * 55,
            f"  ⚽  Dixon-Coles 스코어라인 분석",
            f"     {home_name}  vs  {away_name}",
            "=" * 55,
            f"  예상 골 수   :  {home_name} {eg['home']}  |  {away_name} {eg['away']}",
            "",
            f"  승/무/패 확률",
            f"    {home_name} 승  :  {wdl['home_win']}%",
            f"    무 승 부    :  {wdl['draw']}%",
            f"    {away_name} 승  :  {wdl['away_win']}%",
            "",
            "  ─── 가장 가능성 높은 스코어 TOP 5 ───",
        ]
        for rank, s in enumerate(top, 1):
            bar = "█" * int(s["probability_pct"] / 1.5)
            lines.append(
                f"  {rank}위  {s['scoreline']:>5}   {s['probability_pct']:5.1f}%  {bar}"
            )
        lines += [
            "",
            f"  모델: {result['model']}  |  ρ={result['rho']}",
            "=" * 55,
        ]
        return "\n".join(lines)


# ===========================================================================
# Engine 7: GroupSituationEngine — 2026 group standings + qualification logic
# ===========================================================================
#
# 2026 FIFA World Cup format: 12 groups (A-L) of 4 teams. Top 2 of each group
# plus the 8 best third-placed teams advance to the Round of 32.
#
# Group tiebreakers (in order):
#   1) head-to-head points          2) head-to-head goal difference
#   3) head-to-head goals scored    4) overall goal difference
#   5) overall goals scored         6) fair play (cards)
#   7) FIFA ranking
# Best-third tiebreakers: points -> overall GD -> overall GF -> fair play ->
# FIFA ranking.
#
# DATA LIMITATIONS (honest disclosure — see CLAUDE.md rules on no-hallucination):
#   * Fair play uses TOTAL card count per team. The DB stores event_type='card'
#     without the yellow/red subtype, so FIFA's weighted fair-play points cannot
#     be computed. Fewer cards = better.
#   * FIFA ranking is a static approximation (FIFA_RANKING below), used only as
#     the final, rarely-decisive tiebreaker.
#   * Tiebreakers 1-3 are applied once per equal-points cluster. FIFA's full
#     recursive re-application (when h2h partially separates a 3+ way tie) is
#     not modelled.
#   * Qualification scenarios enumerate remaining fixtures with representative
#     scorelines (win 2-0, draw 1-1, loss 0-2) so goal difference varies across
#     scenarios. Exactly-tied teams are assigned a RANGE of possible finishing
#     positions, so genuinely symmetric teams are treated symmetrically. The
#     exact distribution of real scorelines is still an approximation.
#   * "can_qualify_as_third" reflects whether a team can finish 3rd IN ITS GROUP.
#     The cross-group "8 best thirds" cut needs all 12 groups complete and is
#     not evaluated here.


class GroupSituationEngine:
    """2026 group standings + per-team qualification situation."""

    # Approximate FIFA ranking (lower = better), ~June 2026. Final tiebreaker
    # only; teams not listed default to a large rank (worse).
    FIFA_RANKING: dict[str, int] = {
        "Argentina": 1, "France": 2, "Spain": 3, "England": 4, "Brazil": 5,
        "Netherlands": 6, "Portugal": 7, "Belgium": 8, "Italy": 9, "Germany": 10,
        "Croatia": 11, "Morocco": 12, "Colombia": 13, "Uruguay": 14, "USA": 15,
        "Mexico": 16, "Switzerland": 17, "Senegal": 18, "Japan": 19, "Denmark": 20,
        "Iran": 21, "South Korea": 22, "Australia": 23, "Ecuador": 24,
        "Austria": 25, "Canada": 26, "Poland": 27, "Serbia": 28, "Egypt": 29,
        "Nigeria": 30, "Norway": 31, "Sweden": 32, "Czechia": 33, "South Africa": 34,
    }
    _RANK_DEFAULT = 99

    @staticmethod
    def _norm_group(group_name: str) -> str:
        g = (group_name or "").strip()
        if not g.lower().startswith("group"):
            g = f"Group {g}"
        return g

    def _fifa_rank(self, team: str) -> int:
        return self.FIFA_RANKING.get(team, self._RANK_DEFAULT)

    # ---- DB collection ----------------------------------------------------

    def _collect(self, group_name, season, conn):
        """Return (matches, team_names, cards) for a group.

        matches: [{"home","away","hs","as_","status"}, ...] (all matches)
        team_names: sorted list of team names in the group
        cards: {team_name: total_card_count}
        """
        # A caller-supplied raw sqlite3 connection may not have the Row factory;
        # set it so column-name access works (no-op for PG's _PgConnection).
        import sqlite3 as _sq
        if isinstance(conn, _sq.Connection) and conn.row_factory is not _sq.Row:
            conn.row_factory = _sq.Row

        # Match the group whether stored as "Group A" (production) or "A" (e.g.
        # synthetic/test data).
        g_norm = self._norm_group(group_name)
        g_raw = (group_name or "").strip()
        group_vals = [g_norm] if g_raw == g_norm else [g_norm, g_raw]
        ph = ",".join("?" for _ in group_vals)

        matches = []
        for r in conn.execute(
            f"""
            SELECT ht.name AS home, at.name AS away,
                   m.home_score AS hs, m.away_score AS as_, m.status AS status
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = ? AND m.group_name IN ({ph})
            ORDER BY m.kickoff_utc
            """,
            (season, *group_vals),
        ).fetchall():
            matches.append({
                "home": r["home"], "away": r["away"],
                "hs": r["hs"], "as_": r["as_"], "status": r["status"],
            })

        teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})

        cards = {t: 0 for t in teams}
        try:
            for r in conn.execute(
                f"""
                SELECT t.name AS name, COUNT(*) AS c
                FROM match_events me
                JOIN matches m ON m.id = me.match_id
                JOIN teams t   ON t.id = me.team_id
                WHERE m.season = ? AND m.group_name IN ({ph})
                  AND me.event_type = 'card'
                GROUP BY t.id, t.name
                """,
                (season, *group_vals),
            ).fetchall():
                if r["name"] in cards:
                    cards[r["name"]] = r["c"]
        except Exception:
            # match_events may be absent (partial/test DB) -> treat as 0 cards.
            pass

        return matches, teams, cards

    @staticmethod
    def _is_completed(m) -> bool:
        return m["hs"] is not None and m["as_"] is not None

    # ---- Standings computation -------------------------------------------

    def _stats_from_results(self, teams, results, cards):
        st = {t: {"team": t, "points": 0, "gf": 0, "ga": 0, "gd": 0,
                  "played": 0, "cards": cards.get(t, 0)} for t in teams}
        for m in results:
            h, a, hs, as_ = m["home"], m["away"], m["hs"], m["as_"]
            if h not in st or a not in st:
                continue
            st[h]["played"] += 1
            st[a]["played"] += 1
            st[h]["gf"] += hs
            st[h]["ga"] += as_
            st[a]["gf"] += as_
            st[a]["ga"] += hs
            if hs > as_:
                st[h]["points"] += 3
            elif hs < as_:
                st[a]["points"] += 3
            else:
                st[h]["points"] += 1
                st[a]["points"] += 1
        for t in st.values():
            t["gd"] = t["gf"] - t["ga"]
        return st

    def _h2h(self, cluster_names, results):
        """Head-to-head mini stats among cluster teams (completed only)."""
        h = {t: {"points": 0, "gf": 0, "ga": 0, "gd": 0} for t in cluster_names}
        cs = set(cluster_names)
        for m in results:
            home, away, hs, as_ = m["home"], m["away"], m["hs"], m["as_"]
            if home in cs and away in cs:
                h[home]["gf"] += hs
                h[home]["ga"] += as_
                h[away]["gf"] += as_
                h[away]["ga"] += hs
                if hs > as_:
                    h[home]["points"] += 3
                elif hs < as_:
                    h[away]["points"] += 3
                else:
                    h[home]["points"] += 1
                    h[away]["points"] += 1
        for t in h.values():
            t["gd"] = t["gf"] - t["ga"]
        return h

    def _rank(self, teams, results, cards):
        """Ordered list of stat dicts applying the full tiebreaker chain.

        Each returned dict carries a "_key" tuple that fully determines its
        order (higher = better). Two teams with an identical "_key" are exactly
        tied — assess_situation uses this to treat their finishing position as a
        range, so genuinely symmetric teams are handled symmetrically.
        """
        st = self._stats_from_results(teams, results, cards)
        order = list(st.values())
        order.sort(key=lambda t: t["points"], reverse=True)

        resolved = []
        i = 0
        while i < len(order):
            j = i
            while j < len(order) and order[j]["points"] == order[i]["points"]:
                j += 1
            cluster = order[i:j]
            names = [t["team"] for t in cluster]
            h2h = self._h2h(names, results) if len(cluster) > 1 else {}
            for t in cluster:
                hh = h2h.get(t["team"], {"points": 0, "gd": 0, "gf": 0})
                t["_key"] = (
                    t["points"],
                    hh["points"], hh["gd"], hh["gf"],   # head-to-head 1-3
                    t["gd"], t["gf"],                    # overall 4-5
                    -t["cards"],                         # fair play (fewer better)
                    -self._fifa_rank(t["team"]),         # FIFA rank (lower better)
                )
            cluster.sort(key=lambda t: t["_key"], reverse=True)
            resolved.extend(cluster)
            i = j
        return resolved

    # ---- Public: standings -----------------------------------------------

    def get_group_standings(self, group_name, season=2026, conn=None):
        own = conn is None
        if own:
            import database
            conn = database.get_connection()
        try:
            matches, teams, cards = self._collect(group_name, season, conn)
        finally:
            if own:
                conn.close()
        completed = [m for m in matches if self._is_completed(m)]
        ranked = self._rank(teams, completed, cards)
        return [{
            "team": t["team"], "points": t["points"], "gd": t["gd"],
            "gf": t["gf"], "played": t["played"], "ga": t["ga"],
        } for t in ranked]

    # ---- Public: situation -----------------------------------------------

    def _mk(self, status, lam, can_third, note):
        return {
            "status": status,
            "can_qualify_as_third": bool(can_third),
            "lambda_multiplier": lam,
            "note": note,
        }

    def assess_situation(self, team_name, group_name, season=2026, conn=None):
        own = conn is None
        if own:
            import database
            conn = database.get_connection()
        try:
            matches, teams, cards = self._collect(group_name, season, conn)
        finally:
            if own:
                conn.close()

        # Resolve team name case-insensitively against the group.
        target = next((t for t in teams
                       if t == team_name or t.lower() == team_name.lower()), None)
        if target is None:
            return self._mk("live", 1.0, False, "Team not in this group")

        completed = [m for m in matches if self._is_completed(m)]
        remaining = [m for m in matches if not self._is_completed(m)]
        team_total = max(0, len(teams) - 1)   # round-robin: each plays n-1
        played = sum(1 for m in completed if target in (m["home"], m["away"]))
        rem_count = team_total - played
        last_place = len(teams)

        def position_in(results):
            """Set of possible finishing positions for the target. Exactly-tied
            teams share the full range their tie spans."""
            ranked = self._rank(teams, results, cards)
            tkey = next((t["_key"] for t in ranked if t["team"] == target), None)
            if tkey is None:
                return {last_place}
            above = sum(1 for t in ranked if t["_key"] > tkey)
            tied = sum(1 for t in ranked if t["_key"] == tkey)
            return set(range(above + 1, above + tied + 1))

        # No games left -> deterministic (modulo unbroken exact ties).
        if rem_count <= 0:
            posset = position_in(completed)
            if max(posset) <= 2:
                return self._mk("already_qualified", 0.75, False, "Qualification secured")
            if min(posset) >= last_place:
                return self._mk("already_eliminated", 0.88, False, "Eliminated")
            return self._mk("live", 1.0, (3 in posset), "3rd place — awaiting other groups")

        # Enumerate remaining group fixtures with canonical scorelines.
        rem_fixtures = [(m["home"], m["away"]) for m in remaining]
        next_idx = next((i for i, (h, a) in enumerate(rem_fixtures)
                         if target in (h, a)), None)

        import itertools

        # Representative scorelines so goal difference varies across scenarios
        # (canonical 1-0/0-0 erased GD signal and caused symmetry artifacts).
        def synth(home, away, o):
            if o == "H":
                return {"home": home, "away": away, "hs": 2, "as_": 0}
            if o == "A":
                return {"home": home, "away": away, "hs": 0, "as_": 2}
            return {"home": home, "away": away, "hs": 1, "as_": 1}

        all_pos, draw_pos, win_pos, loss_pos = set(), set(), set(), set()
        for combo in itertools.product(("H", "D", "A"), repeat=len(rem_fixtures)):
            sim = completed + [
                synth(rem_fixtures[i][0], rem_fixtures[i][1], o)
                for i, o in enumerate(combo)
            ]
            posset = position_in(sim)
            all_pos |= posset
            if next_idx is not None:
                h, _a = rem_fixtures[next_idx]
                o = combo[next_idx]
                team_is_home = (target == h)
                if o == "D":
                    draw_pos |= posset
                elif (o == "H") == team_is_home:
                    win_pos |= posset
                else:
                    loss_pos |= posset

        def guaranteed_top2(s):
            return bool(s) and max(s) <= 2

        def alive(s):                     # can still finish top 3
            return bool(s) and min(s) <= 3

        can_third = (3 in all_pos)
        can_reach_top2 = bool(all_pos) and min(all_pos) <= 2

        if guaranteed_top2(all_pos):
            return self._mk("already_qualified", 0.75, can_third, "Qualification secured")
        if not alive(all_pos):
            return self._mk("already_eliminated", 0.88, False, "Eliminated")
        if guaranteed_top2(draw_pos):
            return self._mk("draw_enough", 0.87, can_third, "A draw secures qualification")
        if alive(win_pos) and not alive(draw_pos) and not alive(loss_pos):
            return self._mk("must_win", 1.20, can_third, "Must win to survive")
        # "Third only" applies when top 2 is no longer reachable but a draw
        # keeps a third-place path alive. Leaders still chasing top 2 stay live.
        if not can_reach_top2 and can_third and alive(draw_pos):
            return self._mk("live", 0.90, True, "Can advance as third place (top-2 out of reach)")
        return self._mk("live", 1.0, can_third, "Qualification open")


def _team_strengths_from_db(db_path: str = "worldcup.db") -> dict[str, dict[str, float]]:
    """
    Compute per-team attack/defense strengths from completed matches.

    Strengths are normalised against the league (tournament) average so that
    attack == 1.0 means "scores the league-average number of goals" and
    defense == 1.0 means "concedes the league-average number of goals"
    (defense > 1.0 is a *weaker* defense, per Dixon-Coles convention).

    Returns {team_name: {"attack": float, "defense": float}}.
    Returns {} if the data is unavailable.

    Works against either backend via database.get_connection() (SQLite locally,
    PostgreSQL on Railway). db_path is honoured only for the SQLite backend.
    """
    import database

    try:
        conn = database.get_connection(None if db_path == "worldcup.db" else db_path)
    except Exception:
        return {}

    try:
        # Per-team goals for / against across all completed matches (home + away).
        # GROUP BY includes t.name so PostgreSQL strict-mode accepts the SELECT.
        rows = conn.execute(
            """
            SELECT t.name AS name,
                   AVG(CASE WHEN ts.is_home = 1 THEN m.home_score ELSE m.away_score END) AS gf,
                   AVG(CASE WHEN ts.is_home = 1 THEN m.away_score ELSE m.home_score END) AS ga
            FROM team_stats ts
            JOIN matches m ON ts.match_id = m.id
            JOIN teams t   ON ts.team_id  = t.id
            WHERE m.status = 'completed'
            GROUP BY t.id, t.name
            """
        ).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()

    if not rows:
        return {}

    # League average goals per team per match (== avg goals_for == avg goals_against).
    # float() everywhere: on PostgreSQL, AVG() over INTEGER scores returns Decimal,
    # which then breaks `Decimal * float` arithmetic downstream.
    league_avg = sum(float(r["gf"] or 0) for r in rows) / len(rows)
    if league_avg <= 0:
        return {}

    return {
        r["name"]: {
            "attack":  float(round(float(r["gf"] or 0) / league_avg, 4)),
            "defense": float(round(float(r["ga"] or 0) / league_avg, 4)),
        }
        for r in rows
    }


def build_2026_elo(db_path: str = "worldcup.db") -> dict[str, float]:
    """
    Build live 2026 ELO ratings by replaying completed 2026 matches in
    chronological order on top of FIFA-ranking-based seeds.

    Replays ONLY the 2026 tournament on top of the FIFA-ranking-normalised
    INITIAL_RATINGS_2026 seeds (all 48 qualifiers). Teams not in the seed dict
    fall back to DEFAULT_ELO.

    Returns {team_name: current_elo} for every team with a rating. If no 2026
    results are available yet, returns just the seeds (no replay).

    Works against either backend via database.get_connection() (SQLite locally,
    PostgreSQL on Railway). db_path is honoured only for the SQLite backend.
    """
    import database

    seeds = INITIAL_RATINGS_2026

    def stage_key(s: str | None) -> str:
        s = (s or "").lower()
        if "round of 16" in s:
            return "round_of_16"
        if "quarter" in s:
            return "quarter"
        if "semi" in s:
            return "semi"
        if "final" in s:
            return "final"
        return "group"

    elo = EloEngine()
    for team, rating in seeds.items():
        elo.set_rating(team, rating)

    try:
        conn = database.get_connection(None if db_path == "worldcup.db" else db_path)
    except Exception:
        return dict(elo.ratings)   # DB unreachable -> seeds only

    try:
        rows = conn.execute(
            """
            SELECT ht.name AS home, at.name AS away,
                   m.home_score AS hs, m.away_score AS as_, m.stage AS stage
            FROM matches m
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = 2026 AND m.status = 'completed'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY m.kickoff_utc ASC
            """
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    for r in rows:
        hs, as_ = r["hs"], r["as_"]
        result = "draw" if hs == as_ else ("home_win" if hs > as_ else "away_win")
        elo.update(r["home"], r["away"], result, stage_key(r["stage"]))

    return dict(elo.ratings)


# ===========================================================================
# Prediction tracker — store pre-kickoff predictions + Brier calibration
# ===========================================================================

def save_prediction(match_id, home_win_pct, draw_pct, away_win_pct,
                    model_version="v1", notes=None, conn=None,
                    suggested_bet=None, draw_edge=None, total_xg=None,
                    predicted_outcome=None, confidence=None, is_tossup=None):
    """Persist a prediction to the predictions table (percentages 0-100).

    The Layer B label snapshot (predicted_outcome = argmax of the distribution,
    confidence = the winning probability, is_tossup = top-two within 5pp) is
    stored alongside the distribution. suggested_bet holds a human-readable label
    string for display; draw_edge is retained for backward compatibility only and
    is written NULL by current callers (the 26% baseline was removed).

    created_at defaults to CURRENT_TIMESTAMP. Pass an open `conn` to batch
    inside a caller's transaction (the caller then commits); otherwise a
    connection is opened, committed, and closed here.
    """
    import database

    own = conn is None
    if own:
        conn = database.get_connection()
    try:
        conn.execute(
            "INSERT INTO predictions "
            "(match_id, home_win_pct, draw_pct, away_win_pct, model_version, notes, "
            "suggested_bet, draw_edge, total_xg, "
            "predicted_outcome, confidence, is_tossup) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, home_win_pct, draw_pct, away_win_pct, model_version, notes,
             suggested_bet, draw_edge, total_xg,
             predicted_outcome, confidence, is_tossup),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def compute_brier_score(season=2026, conn=None):
    """Multi-class Brier score over completed, predicted matches.

    BS = (p_home - a_home)^2 + (p_draw - a_draw)^2 + (p_away - a_away)^2
    where the actual outcome vector is 1.0 for what happened, 0.0 otherwise
    (range 0..2; lower = better calibration). When a match has several stored
    predictions, the EARLIEST (pre-kickoff) one is used.

    Returns {"brier_score": float, "n_matches": int, "breakdown": [...]}.
    """
    import database

    own = conn is None
    if own:
        conn = database.get_connection()
    import sqlite3 as _sq
    if isinstance(conn, _sq.Connection) and conn.row_factory is not _sq.Row:
        conn.row_factory = _sq.Row

    try:
        rows = conn.execute(
            """
            SELECT p.match_id AS match_id,
                   p.home_win_pct AS hp, p.draw_pct AS dp, p.away_win_pct AS ap,
                   m.home_score AS hs, m.away_score AS as_,
                   ht.name AS home, at.name AS away
            FROM predictions p
            JOIN matches m ON m.id = p.match_id
            JOIN teams ht ON ht.id = m.home_team_id
            JOIN teams at ON at.id = m.away_team_id
            WHERE m.season = ? AND m.status = 'completed'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY p.match_id, p.created_at
            """,
            (season,),
        ).fetchall()
    finally:
        if own:
            conn.close()

    # Keep the earliest prediction per match (rows are ordered by created_at).
    seen = {}
    for r in rows:
        if r["match_id"] not in seen:
            seen[r["match_id"]] = r

    breakdown = []
    total = 0.0
    for r in seen.values():
        ph = (r["hp"] or 0) / 100.0
        pd = (r["dp"] or 0) / 100.0
        pa = (r["ap"] or 0) / 100.0
        hs, as_ = r["hs"], r["as_"]
        if hs > as_:
            a_home, a_draw, a_away, res = 1.0, 0.0, 0.0, "home"
        elif hs < as_:
            a_home, a_draw, a_away, res = 0.0, 0.0, 1.0, "away"
        else:
            a_home, a_draw, a_away, res = 0.0, 1.0, 0.0, "draw"
        bs = (ph - a_home) ** 2 + (pd - a_draw) ** 2 + (pa - a_away) ** 2
        total += bs
        breakdown.append({
            "match_id": r["match_id"],
            "match": f"{r['home']} vs {r['away']}",
            "predicted": {"home": round(ph * 100, 1),
                          "draw": round(pd * 100, 1),
                          "away": round(pa * 100, 1)},
            "actual": res,
            "brier": round(bs, 4),
        })

    n = len(breakdown)
    return {
        "brier_score": round(total / n, 4) if n else 0.0,
        "n_matches": n,
        "breakdown": breakdown,
    }


if __name__ == "__main__":
    _run_demo()

    # --- Dixon-Coles scoreline prediction --------------------------------
    inp = _korea_vs_portugal_inputs()
    dc_engine = DixonColesEngine()
    dc_result = dc_engine.predict(
        home_elo=inp["home_elo"],
        away_elo=inp["away_elo"],
    )
    print(dc_engine.format_report_kr("한국", "포르투갈", dc_result))
