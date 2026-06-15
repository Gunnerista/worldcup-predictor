# MATCHIQ
### FIFA World Cup 2026 — Statistical Match Prediction System
> Dixon-Coles Bivariate Poisson · ELO Rating System · MLE Parameter Estimation · Real-time Data Pipeline

[![Python](https://img.shields.io/badge/Python-3.14-blue)]()
[![Flask](https://img.shields.io/badge/Flask-3.x-green)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Railway-blue)]()
[![Live](https://img.shields.io/badge/Live-Railway-brightgreen)](https://worldcup-predictor-production-c55a.up.railway.app)

## Overview

Statistical match outcome prediction system for the 2026 FIFA World Cup. Implements Dixon-Coles bivariate Poisson modeling with MLE-estimated parameters, real-time ELO updates from match results, and automated post-match accuracy tracking across all 104 tournament matches.

**Live:** https://worldcup-predictor-production-c55a.up.railway.app

| Metric | Value |
|--------|-------|
| Model | Dixon-Coles Bivariate Poisson |
| ρ (rho) | -0.1514 (MLE, n=128 World Cup matches) |
| ELO seeds | FIFA official rankings, June 2026 |
| λ correction layers | 5 |
| Matches tracked | 104 (full tournament) |
| Brier Score | Updating live — see `/api/brier` |

## Mathematical Model

The core model computes joint scoreline probabilities using Dixon-Coles bivariate Poisson:

```
P(X=i, Y=j) = τ_ρ(i,j) · (e^-λ · λⁱ / i!) · (e^-μ · μʲ / j!)
```

`τ_ρ(i,j)` is the low-score correction factor (Dixon & Coles, 1997):

```
τ_ρ(i,j) = 1 - λμρ    if i=0, j=0
         = 1 + λρ     if i=0, j=1
         = 1 + μρ     if i=1, j=0
         = 1 - ρ      if i=1, j=1
         = 1          otherwise
```

**ρ = -0.1514** — estimated via golden-section search maximizing the log-likelihood on 128 World Cup matches (2018 + 2022). Not borrowed from EPL literature — the World Cup produces different scoring patterns.

## 5-Layer λ Correction Pipeline

Goal expectancy (λ, μ) is computed through 5 sequential adjustments:

```
λ_final = base_attack
        × elo_weight       [Layer 1: ELO strength ratio, clamped 0.6–1.67]
        × away_defense     [Layer 2: DB-derived team strength, 2026 data only]
        × player_xG_adj    [Layer 3: recent lineup xG vs team average, 0.7–1.4]
        × situation_mult   [Layer 4: group-stage qualification status]
        × notes_adj        [Layer 5: user-input key player absences / tactical notes]
```

Layer 4 multipliers (Group Situation Engine):

| Situation | Multiplier | Rationale |
|-----------|-----------|-----------|
| `already_qualified` | ×0.75 | rotation expected |
| `draw_enough` | ×0.87 | defensive setup |
| `live` | ×1.00 | baseline |
| `must_win` | ×1.20 | attacking intent |
| `already_eliminated` | ×0.88 | low motivation |

## Architecture

```
BALLDONTLIE API
└── data_pipeline.py
    ├── sync_live_lite()       # 60s polling — scores + status
    ├── enrich_completed()     # player_stats, shots, events (post-match)
    └── Background thread      # auto-triggers on match completion

PostgreSQL (Railway)
└── 11 tables: matches, teams, players, team_stats,
               player_stats, match_shots, match_momentum,
               match_events, predictions, user_notes,
               polymarket_odds

Flask App (app.py)
├── /            — fixture list with date grouping (EST)
├── /match/<id>  — pre / post match analysis
└── /api/brier   — live Brier Score JSON

model.py — 7 engines:
├── EloEngine            # ELO with stage-aware K-factor
├── TacticalEngine       # possession, altitude, zone weakness
├── PlayerMatchupEngine  # xG impact scoring, fatigue
├── PatternMatcher       # historical similarity search
├── NarrativeEngine      # analysis text generation
├── DixonColesEngine     # bivariate Poisson + MLE ρ
└── GroupSituationEngine # 2026 format rules, scenario enumeration
```

## Key Technical Decisions

**Why ρ from World Cup data, not EPL:**
ρ captures sport-level low-score dependency — a property of football itself, not of specific teams or managers. Historical World Cup data (2018 + 2022) is appropriate for this parameter even though team-level strength data is not.

**Why 2026-only data for team strength:**
Attack/defense strength parameters reflect the current squad, manager, and tactical setup. Post-2022, most major teams changed managers — using 2018/2022 data would introduce systematic bias.

**ELO initialization:**
Seeds from official FIFA ranking points (June 2026), normalized as `ELO = 1500 + (FIFA_pts − 1500) × 0.8`. Updated after every completed match using stage-aware K-factors (Group: 20, R32: 30, QF: 40, SF/F: 50).

**PostgreSQL Decimal compatibility:**
PostgreSQL returns `AVG()` as `decimal.Decimal`. All DB-derived numeric values are explicitly cast to `float()` before arithmetic — discovered and fixed after Railway deployment (local SQLite returns float, which masked the issue).

## Post-Match Accuracy Tracking

Every match prediction is stored pre-kickoff and evaluated post-match:

- **Brier Score** — `BS = Σ(pᵢ − oᵢ)²` across win/draw/loss outcomes (0–2 scale, lower is better)
- **Winner accuracy** — predicted vs actual match winner
- **Season record** — cumulative correct predictions

Live accuracy: `/api/brier`

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.14, Flask |
| Database | PostgreSQL (Railway) |
| Data source | BALLDONTLIE FIFA API |
| Deployment | Railway (auto-deploy from GitHub) |
| Statistical model | Dixon-Coles (1997), custom MLE |
| ELO system | Custom implementation, FIFA-seeded |
