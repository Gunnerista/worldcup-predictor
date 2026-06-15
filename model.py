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


if __name__ == "__main__":
    _run_demo()
