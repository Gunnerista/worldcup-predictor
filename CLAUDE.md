# CLAUDE.md — MATCHIQ (worldcup-predictor)

이 파일은 프로젝트 단일 진실 공급원(SSoT). 컨텍스트가 꽉 차서 새 세션을 열어도, 이 문서만 읽으면 그대로 이어갈 수 있어야 함.

> **새 세션 필독:** §11 함정(Gotchas) 먼저 읽어. 같은 함정에 두 번 빠지지 마.
> **최종 갱신:** 2026-06-28 (**Knockout 브래킷 트리 + 우승확률 Monte Carlo** / 32강 placeholder sync 교정 / match 진출% / v1.1+tcal)

---

## 1. 프로젝트 정체성

- **이름:** MATCHIQ (레포명은 여전히 `worldcup-predictor`)
- **목표:** FIFA World Cup 2026 경기 승/무/패 확률을 수학적으로 계산해서 보여주는 예측 시스템.
- **용도:** Polymarket / Kalshi에서 사용자가 **직접** 베팅 결정을 내리는 의사결정 보조 도구.
- **자동 베팅 절대 금지.** 코드 어디에도 거래소 주문 API 호출 로직 넣지 말 것.
- **브랜드/UI:** "MATCHIQ" — 통계 기반 분석 콘솔. **UI 전부 영어, Home/Away 레이블 없이 팀명으로 직접 표시.** 국기 이모지 + 팀 컬러.

---

## 2. 운영자 컨텍스트

- 운영자는 **비코더**. 코드 라인 단위 검수 못 함.
- **운영자와의 대화는 한국어**, 단 **웹사이트 UI 텍스트는 전부 영어** (사용자 요구). 코드/커밋/주석/로그도 영어.
- 외부 API 동작은 검증 후 단언. "아마 될 거예요" 금지.
- 실거래/실머니 영향 코드는 무조건 테스트.
- 글로벌 규칙은 `~/.claude/CLAUDE.md` 참고. 충돌 시 본 파일 우선.

---

## 3. 현재 상태 (Live)

- **GitHub:** `https://github.com/Gunnerista/worldcup-predictor.git` (branch: `main`)
- **라이브:** `https://worldcup-predictor-production-c55a.up.railway.app` (Railway, GitHub push 시 자동 배포)
- **로컬 DB:** `worldcup.db` (SQLite) — 2018+2022 128경기 + **2026 104경기** 포함, gitignored
- **Railway DB:** PostgreSQL — 스키마 자동 생성. 데이터는 `migrate.py`로 옮겨야 채워짐 (또는 백그라운드 sync가 2026 데이터를 직접 적재)
- **MATCHIQ 리브랜드 완료**: 영어 UI, 날짜별 사이드바, PRE/POST 모드, DixonColes 확률, 국기, **Eastern(DST 자동) 시간**.

### 3.1 마지막 세션 인수인계 (2026-06-28 — Knockout 브래킷 + 우승확률)
**이번 세션 (knockout 진출/우승 예측 + 32강 placeholder 교정 + 웹 브래킷 + match 진출%):** 단일 커밋으로 push+Railway 배포.
- **BUG (32강 placeholder 고착) — 수정.** `data_pipeline.py sync_live_lite`가 score/status만 UPDATE하고 `home/away_team_id`는 안 건드림 + `upsert_matches`=`INSERT OR IGNORE` → R32 경기가 "2A vs 2B"로 영구 고착(BALLDONTLIE API는 실제 팀 South Africa-Canada 등 정상 제공, `match_number` 필드는 전부 6으로 깨짐·신뢰불가). **수정: sync UPDATE에 `home_team_id=COALESCE(?,home_team_id)`, `away_team_id=COALESCE(?,…)` 추가** → 다음 sync에 라이브 16경기 실제 팀 자동 교정(COALESCE라 미정 슬롯 보존). 로컬 검증 placeholder 0/16.
- **데이터 품질 검증.** 진출 32팀 전부 조별 3경기 완료, team_stats+xG 100%(96/96). **2026 player_stats는 불완전**(France 0건 — sync가 rate-limit로 player stats 스킵) → `_player_xg_adjustment` 32팀 전부 **1.0(중립)**. 즉 현재 모든 예측 = **순수 팀 데이터**(골/xG/ELO/strength), 선수/부상/감독 0 개입.
- **Knockout 엔진 (`app.py _compute_knockout`, MC 20k).** 진출확률 = **win + 0.5*draw**(calibrated; 연장/승부차기=동전던지기, 모델링 안 함). 검증: 2018/2022 knockout 백테스트 Brier **0.2216**(동전 0.25)·문헌(Csató 2025 승부차기≈coinflip, Groll ET) 일치 — 단순 A가 학술 B(ET×⅓ 시뮬)와 동등. 브래킷 wiring = API source "W##" 라벨에서 복원(R32 kickoff순=공식 match# 73-88, R16 89-96…). **완료경기는 실제 승자 고정**(146 Canada). 캐시 `_knockout_cache_result`(sync ELO 무효화 시 같이 클리어).
- **`/knockout` 페이지 (knockout.html).** 토너먼트 **브래킷 트리**(좌→우 modal 경로, leaf 정렬 expand()) + 우승확률 보드 + 정직성 footer. **트로피=우승확률 1위(board[0]), NOT modal champ** — modal 단일경로 우승자와 MC 1위가 다를 수 있어(France 11.8 vs Brazil 10.8 동급) 혼란 방지차 트로피는 board[0]로 통일. 네비 "Bracket →"(index.html).
- **match knockout 진출%.** `_build_pre_match_bundle`: `is_knockout = group_name is None`, `adv1=round(cwh+cwd/2)`, `adv2=100-adv1`. match.html **PRE 경로**에서 knockout이면 2-way 진출% 바(무승부 바 제거). **POST/group 무변경**(스냅샷·회귀 없음). 내러티브/MODEL PREDICTION은 W/D/L 기반 유지(정규시간 맥락 — 후속 통일 가능).
- **미추적 임시:** `diag_knockout.py`/`diag_bugs.py`/`diag_smoke.py`/`diag_verify.py`/`verify_layerb.py` — 커밋 안 함.
- **다음:** match knockout 내러티브/예측 라벨 통일, parametric bootstrap(챔피언 과신 보정 — 문헌 권고), 시장(Polymarket) 대비 RPS 트래킹, lineup(`/match_lineups` 존재·`/injuries` 404) 기반 결장 자동감지(킥오프 ~1h 전).

---

**이전 세션 (2026-06-20 — v1.1+tcal 5버그 수정):**
**(strength shrinkage + λ cap + upset 재정의 + draw 분포 검증):** 진단 5버그 수정, 단일 커밋 `35bea2e`(`fix(model): shrinkage, xG cap, upset prob, attack floor — v1.1+tcal`) push+Railway 배포+라이브 검증 완료.
- **BUG1/BUG4 (동일 뿌리 = 저표본 strength).** `model.py _team_strengths_from_db`가 경기 1~2개로 산출한 attack/defense를 극단값(Haiti attack **0.0**, 1경기 0득점)으로 뱉어 λ를 왜곡 → ELO 뒤집고 draw 부풀림. **수정: 경기수 기반 신뢰도 shrinkage**(n=1→0.3, n=2→0.6, n≥3→1.0)로 중립 1.0 방향 수축: `attack = 1.0 + (raw-1.0)*w`. ELO가 저표본 매치업을 주도하게 됨. 함수는 이제 `games`/`attack_raw`/`defense_raw`도 반환(비파괴). 검증: Haiti 0.0→**0.7**(카드 표시도 정상화). **`_expected_goals`에 ELO override 경고 로그**: `|elo_diff|≥100`인데 strength-driven λ가 ELO 우세팀을 뒤집고 `|lam-mu|≥0.25`일 때만 stderr WARN(=실제 reversal만, 104경기 중 4건 — 노이즈 X).
- **BUG3 (xG cap).** `_expected_goals` + `apply_user_notes` λ/μ 상한 **3.0→5.0**(하한 0.3 유지=양수 보장). MAX_GOALS=6 헤드룸 충분. 검증: 14경기가 새 헤드룸(>3.0) 사용 → 강팀 xG 압축 해제. **주의: "xG For" 카드는 `_match_strength` 원시 평균(cap 없음)** — 운영자가 본 "3.00 cap"은 사실 λ 상한이었음.
- **BUG5 (upset 항상 0%).** 기존 패턴매처 k-NN이 elo_diff를 무시(historical feature가 elo_diff=0 하드코딩→std 무한대→무시)하고 팀 평균 피처로 최근접 이웃 → favorite 매치업에서 거의 0% 고정. **수정: upset = ELO 약체의 모델(캘리브레이션) 승률.** `_build_pre_match_bundle`에서 `upset_pct = round(cwa if elo1≥elo2 else cwh)`. 검증: 31/33/27 = 10/27/24%, 라이브 match/33 = **11%**(데이터 다름, 비0 확인). 내러티브 문구도 갱신. `_pattern_matcher` 전역은 보존(upset 용도만 제거).
- **BUG2 (draw 과대) — 근본 해소, 하드코딩 prior 미적용.** RAW draw max **61.4→44.0**, ELO gap≥100 평균 24.3→**21.2%**(실제 WC ~25% 근접). 극단 draw는 저표본/0.0 strength가 원인이었고 BUG1·4로 자동 해소. **운영자 결정: draw cap(0.35) 미적용** — 기준경기 Brazil/Haiti는 draw 25.4%로 통과, 분포 단조·건강, 검증된 T 캘리브레이션 위에 또 다른 하드 보정 안 쌓음(§11.9 argmax 보존·Layer B의 26% baseline 제거 정신). |gap|≥200 7경기가 31~38% 남으나 전부 draw≠argmax라 변호 가능. **재검토 시: argmax-safe cap(draw가 argmax 아닐 때만 35% 상한, 초과분 home/away 비례 재분배) 구현 가능 — /methodology에 명기 필요.**
- **model_version 컷오버 `v1+tcal`→`v1.1+tcal`** (`CALIBRATION_VERSION`). 거동 변경(shrinkage/cap/upset)으로 저장 분포가 달라지므로 분석상 구분. **기존 저장 스냅샷 불변**(UPDATE/마이그레이션 없음), 신규 예측부터 적용 — 무결성 규칙 유지.
- **미추적 임시:** `diag_bugs.py`/`diag_verify.py`/`diag_smoke.py`(재현용 진단, 커밋 안 함), 기존 `verify_layerb.py`/`backtest_calibration.py`(후자는 이전 세션 커밋됨).
- **다음:** away-favorite richer calibration(보류), draw cap 재검토(보류), README 스크린샷 재촬영, LIVE 실시간 푸시, Monte Carlo 진출 시뮬.

---

**이전 세션 (2026-06-17 — calibration + /methodology):**
**(temperature 캘리브레이션 + 평가 페이지 + 파이프라인 검증):**
- **Temperature 캘리브레이션 (argmax 보존).** 코어 엔진(DC+ρ+ELO/strength)이 꼬리에서 과신 → 단일 `T=1.718`로 W/D/L 분포 평탄화. **`app.py calibrate_wdl(p_home,p_draw,p_away)` = softmax(log(p)/T)**, 0–100 triple 반환, **단조변환이라 argmax(predicted_outcome·SEASON RECORD) 불변, confidence(%)만 완만**(검증 528/528). 삽입 **3곳(라이브 compute만)**: `_build_pre_match_bundle`(match PRE), `index()` today-cards, `_run_due_predictions`(저장). **POST 스냅샷 경로는 미삽입**(저장값 verbatim, 소급 변환 없음). 신규 저장 `model_version="v1+tcal"`(컷오버 마커, 과거 v1/manual 무변경, 스키마 변경 없음).
- **T 산출:** 누수 없는 as-of 백테스트(`backtest_calibration.py`, martj42/international_results CC0, 439경기 2025-07~2026-05) → golden-section NLL(scipy 금지). full-set ECE 0.083→0.026, RPS 0.194→0.185. held-out test(n=102): ECE 0.103→0.027, RPS 0.202 vs base-rate 0.215(climatology 이김). away-favorite 잔여 과신(n=7, gap+0.23) = richer calibration 신호, **미적용**.
- **`/methodology` 평가 페이지** (네비 "Evaluation" 링크). 숫자 전부 `static/calibration_report.json`(백테스트 `--dump` 산출 아티팩트)에서 렌더, **하드코딩 0**. reliability 곡선 = **서버사이드 인라인 SVG**(CDN/JS 의존 없음, viewBox 모바일). 히어로 곡선=전체 439, 지표표=held-out 102(캡션 명시). 정직성 가드: SOTA(0.206) 비교 없음, skill=climatology 우위까지만, 라이브 "X% 적중" 헤드라인 없음, 미검증 2단계(player_xG_adj·situation_mult) 명기.
- **파이프라인 라이브 검증(읽기):** 지난 세션 predictions 2건(14,15) → 현재 **6건(14–19, 자동저장)**, SEASON RECORD **4/6**. match 16–19가 예정→예측저장→완료→POST 채점까지 진행 = 백그라운드 sync(`sync_live_lite`+`_run_due_predictions`) 실행 증거. (직접 heartbeat/created_at·2h·model_version 확인은 Railway 로그/`DATABASE_URL` 필요 — HTTP로는 미관찰.)
- **커밋(전부 push+Railway 배포 검증됨):** `ddac4a8`(temperature calibration), `2b18097`(/methodology page). 라이브 `/`·`/methodology` 200, PRE match 20(Austria/Jordan) raw 46/33/21 → **calibrated 41/33/26** 표시 확인.
- **미추적 임시:** `verify_layerb.py`(Layer B 검증), `backtest_calibration.py`는 **이번에 커밋됨**(재현성, CSV 경로 인자화).
- **다음:** away-favorite richer calibration(보류), README 스크린샷 재촬영, LIVE 실시간 푸시, Monte Carlo 진출 시뮬.

---

**이전 세션 (2026-06-16 — Layer B argmax 라벨링):**
- **Layer B = 순수 argmax 라벨링.** 라벨 = `argmax(p_home, p_draw, p_away)` (최빈 결과 = 예측). **하드코딩 26% draw baseline + 비대칭 문턱(draw>5 / win>55) 전부 제거.** 신뢰도 = 최빈 확률값, `is_tossup` = 상위 2개 확률 차 < 5pp(=0.05, 대칭 플래그). 전체 분포는 계속 계산·저장·표시.
- **개명:** `app.py generate_model_edge` → **`generate_prediction_label`**, 번들 키 `b["model_edge"]` → **`b["prediction"]`**. `_season_record`도 argmax 한 줄로. POST `predicted_winner`는 **저장 분포 스냅샷의 argmax**로 결정(legacy `suggested_bet` 문자열 파싱 제거 — 구 draw 편향 프레이밍 재유입 차단).
- **새 컬럼 3개 (비파괴 ADD):** predictions에 `predicted_outcome TEXT`/`confidence REAL`/`is_tossup INTEGER(0/1)`. `database.py` 스키마 + `_ensure_prediction_edge_columns()` idempotent ALTER. **`draw_edge`/`suggested_bet` 컬럼 보존**(DROP 금지), 단 `draw_edge`는 신규 예측 시 **NULL** 저장(26 baseline 제거). `save_prediction`에 파라미터 3개 추가.
- **UI(match.html):** 'lean'/'bet' 프레이밍 제거 → 중립 통계 **"Expected total goals (xG sum): X.X"**. 라벨+신뢰도+toss-up 칩(PRE/POST). 섹션 제목 "Model Prediction"/"Pre-Match Prediction".
- **PG Decimal→float:** POST에서 `pp1/ppd/pp2 = float(prow[...])` 캐스팅(§11.8).
- **검증(Railway PG `railway run python verify_layerb.py`, SELECT-only):** OLD 규칙 **2/5** → argmax **3/5** (DELTA +1, 구 무승부 편향이 1경기 오판). flip(old=DRAW→argmax≠draw) **3건 중 2건 correct(France, Iraq — argmax 맞고 구 규칙 틀림), 1건 variance(Saudi/Uruguay)**. → 변경이 실효 있음 확인.
- **커밋:** `d0e69a1`(Layer B: replace draw-biased threshold cascade with argmax labeling). **push 보류**(운영자 확인 후 배포). `verify_layerb.py`는 임시·미커밋.
- **다음:** RPS 평가(우선순위 2), strength prior(우선순위 3). strength 엔진·2026 로직은 이번에 미변경.

---

**이전 세션 (2026-06-16):**
1. **"No prediction recorded"** — POST-MATCH에서 predictions에 해당 match_id 행이 없으면 CORRECT/INCORRECT·Brier·구조화 리뷰를 **아예 안 만들고** "No prediction recorded" 표시. (이전엔 결과 후 재계산으로 가짜 적중 판정) — `app.py` `has_prediction = bool(prow)` 분기 + `match.html` `{% if bundle.has_prediction %}`.
2. **Eastern DST 전환** — `EST = timezone(UTC-5)` 고정 → **`EASTERN = ZoneInfo("America/New_York")`**(DST 자동). 여름 EDT(UTC-4)/겨울 EST. 라벨은 `_est_label()`=`tzname()`로 동적("EST"/"EDT"), 하드코딩 "EST" 전부 제거. `requirements.txt`에 **`tzdata` 추가**(Windows zoneinfo 필수, Railware Linux도 안전). **§11.6 EST vs EDT 미결사안 → EDT(America/New_York)로 결정 완료.**
3. **POST "Pre-Match Odds"는 스냅샷-only** — 저장된 예측 없으면 상단 확률 바 자체를 렌더 안 함(완료 경기의 오염된 ELO 재계산값 표시 금지). `app.py` no-snapshot 분기에서 `p1/pdraw/p2=None` + 템플릿 `{% if phase != 'post' or bundle.has_prediction %}`.
4. **Railway PG 예측 수동 INSERT** — match 14(Belgium vs Egypt, 실제 1-1) 예측을 운영자 요청으로 직접 INSERT: `home/draw/away_pct=32/48/19`(0–100 스케일), `created_at='2026-06-15 15:00:00'`(UTC=11:00 EDT), `model_version='manual'`, `suggested_bet='DRAW — 22pp above market average'`, `draw_edge=22.0`, `total_xg=NULL`. → POST에서 **✓ CORRECT**, SEASON RECORD 1/1.

**Railway predictions 상태 (2026-06-17 기준 관측):** **6건** — match 14(manual), 15–19(자동저장). SEASON RECORD **4/6 CORRECT**. match 1–13은 파이프라인 활성화 이전 완료라 "No prediction recorded". **로컬 SQLite는 0건(clean).** 컷오버: ~v1+tcal까지 저장됨, **2026-06-20부터 신규 자동저장은 `v1.1+tcal`**(shrinkage/cap/upset 반영). 기존 행 불변.

**커밋 (전부 push+배포됨):** `35bea2e`(v1.1+tcal: shrinkage/xG cap/upset/attack floor), `2b18097`(/methodology page), `ddac4a8`(temperature calibration T=1.718), `1633869`(docs Layer B), `d0e69a1`(Layer B argmax), `63455fd`(No prediction recorded + Eastern DST), `fc359f8`(POST 스냅샷-only odds).

**⚠️ 남은 일:**
- **DATABASE_URL은 이 dev 환경에 자동 주입 안 됨** — Bash 툴에 인라인(`DATABASE_URL="postgresql://..." python ...`)으로 넘겨야 Railway PG 접근. (운영자가 PowerShell `$env:`로 세팅해도 Bash 서브프로세스엔 전파 안 됨.)
- **Railway PG 실데이터 마이그레이션** 여부 확인(`migrate.py` vs 백그라운드 sync). teams 112/matches 104는 들어가 있음.
- README 스크린샷 2개 커밋됨(`static/screenshot_*.png`) — Eastern/odds 변경 반영하려면 재촬영 필요(선택).

---

## 4. 기술 스택 (실제)

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.14 (Windows 11 로컬, Railway는 Python 3.13) |
| 웹 프레임워크 | Flask 3.x |
| WSGI | gunicorn **`--workers 1`** (Railway), `python app.py` (로컬) |
| 실시간 | 백그라운드 데몬 스레드(sync) + SSE(`/stream`, LIVE 스켈레톤) |
| DB | **SQLite (로컬) + PostgreSQL (Railway)** — `database.py`가 듀얼 백엔드 |
| 프런트 | Jinja2 템플릿 + Vanilla JS — `base.html` 상속 구조 |
| 폰트 | **IBM Plex Mono** (숫자/레이블), system-ui (내러티브 본문) |
| 이모지/국기 | **Twemoji** (CDN) — Windows에서 국기 이모지가 "SE" 같은 글자로 깨지는 것 방지 |
| 시간대 | **US Eastern, DST 자동** (`app.py` `EASTERN = ZoneInfo("America/New_York")`). 여름 EDT(UTC-4)/겨울 EST. 라벨 동적(`_est_label`). `tzdata` 의존성 필요 — §11.6 |
| 디자인 | 다크: bg `#0a0e17` / card `#0f1422` / border `#1a2035`. team1 `#1D9E75`(초록) / team2 `#378ADD`(파랑) / amber `#EF9F27` / text `#e2e8f0` |
| 차트 | Chart.js CDN (LIVE 페이지만, 현재 미주력) |

---

## 5. 데이터 소스

### 5.1 BALLDONTLIE FIFA API
- **베이스 URL:** `https://api.balldontlie.io/fifa/worldcup/v1/`
- **API 키:** `.env` → `BALLDONTLIE_API_KEY` (Railway는 환경변수)
- **⚠️ 실제 rate limit: 분당 5 요청.** `data_pipeline.py` `_get()`이 응답 헤더 `x-ratelimit-remaining`/`reset`을 읽어 동적 페이싱.
- **사용 엔드포인트:** teams, matches, team_match_stats, player_match_stats, match_shots, match_momentum, match_events, rosters
- **2018 데이터엔 team-level xG 없음** (2022/2026은 있음). `match_shots`의 `xg`/`xgot`는 존재.

### 5.2 Polymarket Gamma API
- **URL:** `https://gamma-api.polymarket.com`, 인증 없음
- **⚠️ 경기별 W/D/L 마켓 없음.** 토너먼트 단위만. **현재 MATCHIQ UI엔 표시 안 함** (polymarket.py 코드는 남아있으나 app.py에서 import 제거됨).

---

## 6. 예측 모델 (실제 아키텍처 — `model.py` 7 엔진)

**XGBoost/scipy 안 씀. 순수 Python.**

| 엔진 | 역할 | UI 표시 |
|---|---|---|
| **EloEngine** | 표준 ELO + 무승부 모델(`_DRAW_SIGMA=350`) + 호스트/근접 보너스 | rating 숫자만 |
| **DixonColesEngine** | **메인 모델.** bivariate Poisson + low-score 보정 ρ + 스코어라인 매트릭스 | ✅ W/D/L·xG·스코어라인 |
| **GroupSituationEngine** | 2026 포맷(12조×4팀, 3위 상위 8팀) 순위·타이브레이커 + 잔여일정 brute-force 시나리오 → 진출 상황 | ✅ situation note |
| **PatternMatcher** | z-score 유클리드 유사도 (historical) | ❌ **upset 표시엔 더 이상 안 씀** (v1.1: upset = ELO 약체 모델 승률로 재정의, §6.3) |
| **TacticalEngine** | 약점 zone, 점유율, 고지대 (한국어 출력) | ❌ 미표시 |
| **PlayerMatchupEngine** | 임팩트 스코어, 피로도 (한국어 출력) | ❌ 미표시 |
| **NarrativeEngine** | 한국어 리포트 조립 | ❌ 미표시 (UI 영어라 app.py가 영어 내러티브 생성) |

### 6.1 DixonColes 5단계 λ 보정 (`predict_from_db`)
```
λ_final = base_attack
        × elo_weight       [ELO 강도비, 10**(elo_diff/1600), clamp 0.6–1.67]
        × away_defense     [DB 팀강도 _team_strengths_from_db, 2026 골 기반 + 경기수 shrinkage]
        × player_xG_adj    [_player_xg_adjustment, 최근 라인업 xG/평균, 0.7–1.4]
        × situation_mult   [GroupSituation lambda_multiplier]
        × notes_adj        [apply_user_notes, 가산식]
```
- 최종 lam/mu clamp **`[0.3, 5.0]`** (v1.1: 상한 3.0→5.0, BUG3 — 강팀 xG 압축 해제. 하한 0.3=양수 보장). 결과 dict: `win_draw_loss`, `expected_goals`, `top_scorelines`, `matrix`, `situation`, `player_adjustment`, `notes_adjustment`, `strength_source`.
- **★ strength shrinkage (v1.1, BUG1/BUG4).** `_team_strengths_from_db`가 attack/defense를 **경기수 신뢰도로 중립 1.0 방향 수축**: `1.0 + (raw-1.0)*w`, `w`=n1→0.3·n2→0.6·n≥3→1.0. 저표본 팀(1경기 0득점 → raw attack 0.0)이 ELO를 뒤집는 것 방지(0.0→0.7). 함수가 `games`/`attack_raw`/`defense_raw`도 반환(비파괴). `_expected_goals`는 **ELO override 경고**: `|elo_diff|≥100`인데 λ가 ELO 우세팀 뒤집고 `|lam-mu|≥0.25`면 stderr WARN(실제 reversal만).
- **ρ = -0.1514** — `estimate_rho()`가 2018+2022 128경기로 **MLE(golden-section search)** 추정. scipy 안 씀(순수 Python). `DixonColesEngine.__init__`에서 호출, `_RHO_CACHE`로 1회만. `RHO_SOURCE` = "estimated_from_data"/"fallback_default".

### 6.2 ELO 시드 (FIFA 랭킹 기반)
- **`model.py`의 `FIFA_POINTS_2026`** (48개 본선국, 2026.6 FIFA 포인트) → `INITIAL_RATINGS_2026 = {team: 1500 + (pts-1500)*0.8}` 정규화.
- ⚠️ **app.py가 아니라 model.py에 있음** (이전엔 app.py 임의값이었음 — FIFA 기반으로 교체+이전). app.py는 `from model import INITIAL_RATINGS_2026`.
- `build_2026_elo()`가 이 시드 위에 2026 완료 경기 replay (stage-aware K: group 20 / R32 30 / QF 40 / SF·F 50).
- app.py `_build_live_elo_2026()`가 시드 baseline + build_2026_elo 결과 overlay.

### 6.3 예측 추적 (`model.py` + `app.py`)
- **★ Temperature 캘리브레이션 (후처리, argmax 보존).** 엔진이 W/D/L 분포를 뱉은 **직후** `app.py calibrate_wdl(h,d,a)` = `softmax(log(p)/CALIBRATION_T)`, `CALIBRATION_T=1.718`. 라이브 compute 3곳(PRE 렌더·index 카드·저장)에 적용, **POST 스냅샷 경로는 제외**(저장값 verbatim). 단조라 argmax 불변(confidence만 완만). 신규 저장 `model_version="v1.1+tcal"` (`CALIBRATION_VERSION`; v1.1=shrinkage/cap/upset 컷오버). T 출처·근거 = `backtest_calibration.py` → `static/calibration_report.json`. **§11.9 참고.**
- **★ upset 확률 = ELO 약체의 모델 승률 (v1.1, BUG5).** `_build_pre_match_bundle`에서 `upset_pct = round(cwa if elo1≥elo2 else cwh)` (캘리브레이션 후 분포 사용). 구 PatternMatcher k-NN은 elo_diff 무시(historical feature가 elo_diff=0 하드코딩)+팀 평균 피처라 favorite 매치업에서 거의 **0% 고정** → 폐기. 저장 안 함(표시 전용). 내러티브 문구 "Upset probability (model win chance for the ELO underdog)".
- **`save_prediction()`** — predictions에 킥오프 전 예측 저장. **분포 + Layer B 라벨 스냅샷 저장**: `home/draw/away_win_pct` + `predicted_outcome`/`confidence`/`is_tossup` + `suggested_bet`(표시용 라벨 문자열)/`total_xg`. `draw_edge`는 컬럼 보존하되 신규 저장 시 **NULL**(26 baseline 제거). database.py `_ensure_prediction_edge_columns()`가 init_db에서 idempotent ALTER — 기존 DB도 자동 추가(6개 컬럼).
- **`compute_brier_score(season=2026)`** — 완료+예측 경기 멀티클래스 Brier `Σ(p−o)²`.
- **predicted_winner = 순수 argmax (Layer B).** `argmax(p_home, p_draw, p_away)` — 최빈 결과가 곧 예측. **비대칭 문턱·26% baseline 없음.** 신뢰도 = 최빈 확률, `is_tossup` = 상위 2개 차 < 5pp(=0.05, 대칭). 라벨 산출 = `app.py generate_prediction_label()`(번들 키 `b["prediction"]`). POST는 **저장 분포 스냅샷의 argmax**로 결정(legacy `suggested_bet` 문자열 파싱 안 함 — 구 draw 편향 차단). `_season_record()`도 argmax라 결과 수렴.
- **POST-MATCH 표시 = 경기 전 값.** 상단 확률 바 "Pre-Match Odds"(저장 분포 스냅샷), "Pre-Match Prediction"(저장 분포 argmax 라벨 + 신뢰도 + toss-up + 중립 xG합). 분포 스냅샷 우선 — 완료 경기는 ELO가 결과 반영 후라 재계산값은 진짜 pre-kickoff와 다를 수 있음 → 스냅샷이 정답. 라벨은 그 스냅샷에서 결정적으로 재계산되므로 legacy 행도 백필 없이 정상.
- **`/api/brier`** 라우트 = `{brier_score, n_matches}`.
- **`_run_due_predictions()`** (app.py 백그라운드) — scheduled + 킥오프 2h 전 + 예측 없는 경기만 저장. **이미 in_progress/completed면 재예측 안 함** (한 번 놓치면 그 경기는 예측 없이 끝남).

---

## 7. 웹사이트 구조 (MATCHIQ)

### `base.html`
공통 레이아웃 (title MATCHIQ, IBM Plex Mono, style.css, **Twemoji 스크립트**). index/match가 상속.

### `/` (index.html) — 사이드바 + 메인
- **사이드바(220px):** "MATCHIQ" 로고 + 날짜별(EST) 경기 그룹 (TODAY / JUN 14 …, 최근 7 EST일). 완료=muted "FT 7-1", 예정="14:00 EST", LIVE=amber. 국기 포함.
- **메인:** 오늘(EST) 경기 카드 — 팀명별 DixonColes 확률 + xG + situation note + "View Analysis →".

### `/match/<id>` (match.html) — PRE / POST 모드 (Home/Away 레이블 없음)
- **헤더:** 국기+팀명, ctx_line ("GROUP G · MATCHDAY 1 · 14:00 EST"). POST는 스코어 크게.
- **확률 바:** team1/draw/team2 (인라인 width+background, `<div>` 블록 — §11.7).
- **PRE:** Pre-Match Analysis(영어 내러티브) + 수학블록(λ/μ/ρ/P(0-0)…) + **Model Prediction**(argmax 라벨 + 신뢰도 + toss-up 칩 + 중립 xG합) + Scoreline TOP5(결과별 색) + Team Strength.
- **POST:** Prediction vs Result + 예측 적중 ✓/✗ + Brier + **SEASON RECORD** + **구조화 Post-Match Review**(got_right/missed/key_factors) + Scoreline + Team Strength.

### `/methodology` (methodology.html) — 평가·캘리브레이션 페이지
- 네비 = index 사이드바 `.brand` 아래 "Evaluation →" 링크. 페이지 상단 `← All fixtures`.
- **숫자 전부 `static/calibration_report.json`에서 렌더 (하드코딩 0).** 라우트 `methodology()`가 JSON load → `_build_reliability_svg(hero)`로 SVG 좌표 계산 → 템플릿. JSON 없으면 graceful notice(500 없음).
- 콘텐츠: 제목+서브 / 신뢰 앵커(누수 없는 439) / **히어로 reliability SVG**(raw vs calibrated + 45°, 서버사이드 인라인 SVG·viewBox 모바일) / held-out test 지표표 / 메트릭 카드 3 / 한계(정직) / forward track placeholder / provenance footer(generated_at+git_commit+reproduce).
- **재현:** `INTL_RESULTS_CSV=<results.csv> python backtest_calibration.py --dump` → JSON 재커밋. **Railway엔 CSV 없어 재계산 불가 → JSON 아티팩트가 진실원** (§11.9).

### `/api/brier`
`compute_brier_score` 결과 JSON `{brier_score, n_matches}`. match.html이 fetch.

### 백그라운드 동작 (`app.py` `_live_sync_loop`, import 시 데몬 스레드 기동)
`sync_live_lite()` → (완료 경기 시) `enrich_completed_matches_2026()` + `_elo=None`(재빌드 트리거) → `_run_due_predictions()`(킥오프 2h 전 경기 예측 저장). 라이브 있으면 60s, 없으면 300s. `ENABLE_LIVE_SYNC=0`로 끔.

---

## 8. 파일 구조 (실제)

```
worldcup-predictor/
├── app.py                # Flask 라우트 + 엔진 워밍 + 백그라운드 sync + 영어 내러티브/generate_prediction_label(argmax)/season_record + TEAM_FLAGS + EST
├── model.py              # 7 엔진 + INITIAL_RATINGS_2026/FIFA_POINTS_2026 + estimate_rho + save_prediction + compute_brier_score
├── database.py           # 듀얼 백엔드 + PostgreSQL 호환 레이어(_translate_sql)
├── data_pipeline.py      # BALLDONTLIE 백필/sync (CLI: backfill/sync/today/live/names/events) + sync_live_lite + enrich_completed_matches_2026
├── polymarket.py         # Polymarket Gamma 클라이언트 (현재 UI 미사용)
├── migrate.py            # SQLite → Railway PG 일회성 마이그레이션
├── backtest_calibration.py # 누수 없는 as-of 캘리브레이션 백테스트 → calibration_report.json 덤프
├── Procfile              # web: gunicorn app:app --workers 1
├── requirements.txt
├── templates/
│   ├── base.html         # 공통 레이아웃 + Twemoji
│   ├── index.html        # 사이드바 + 오늘 경기 카드 + Evaluation 링크
│   ├── match.html        # PRE/POST 분석 리포트
│   └── methodology.html  # /methodology 평가·캘리브레이션 페이지 (calibration_report.json 렌더)
├── static/
│   ├── style.css         # MATCHIQ 다크 테마 (IBM Plex Mono)
│   ├── charts.js         # Chart.js + SSE 클라이언트 (LIVE용)
│   ├── calibration_report.json  # 백테스트 산출 아티팩트(= /methodology 진실원). 재생성:
│   │                            #   INTL_RESULTS_CSV=<results.csv> python backtest_calibration.py --dump
│   ├── screenshot_prematch.png   # README용
│   └── screenshot_postmatch.png  # README용
├── README.md             # MATCHIQ 공개 문서 (방법론/아키텍처)
├── .env                  # 로컬 비밀키 (gitignored)
├── .gitignore            # .env, *.db, __pycache__/, *.pyc, .DS_Store, .gstack/
├── CLAUDE.md             # ← 이 파일
└── worldcup.db           # 로컬 SQLite (gitignored)
```

---

## 9. 의존성 (실제 requirements.txt)

```
flask
python-dotenv
requests
gunicorn
psycopg2-binary
tzdata
```

**xgboost/scikit-learn/numpy/pandas/scipy/flask-cors 한 줄도 import 안 함.** MLE(estimate_rho)도 순수 Python golden-section. scipy 추가 금지 — Railway 빌드 깨짐.
**`tzdata`** = 순수 데이터 패키지(빌드 없음). Windows에서 `ZoneInfo("America/New_York")` 해석에 필수(Linux는 시스템 tz DB 있어 선택적이나 명시).

---

## 10. 환경 변수

### 로컬 `.env` (gitignored — git에 절대 안 올라감, 검증됨)
```
BALLDONTLIE_API_KEY=실제키
FLASK_SECRET_KEY=...
FLASK_DEBUG=True
```

### Railway Variables
- `BALLDONTLIE_API_KEY` — 필수
- `DATABASE_URL` — Railway Postgres attach 시 자동 주입. `database.py`가 감지 → PostgreSQL 경로
- `PORT` — Railway 자동 주입
- `DEBUG_TRACEBACK` — 켜면 에러 시 브라우저에 traceback (운영 중 반드시 꺼야 함, 보안 위험)
- `ENABLE_LIVE_SYNC` — 기본 on. 백그라운드 sync 스레드 토글. **워커 여러 개면 한 곳 빼고 0으로** (rate-limit)

---

## 11. 함정 (Gotchas) — **새 세션 필독**

### 11.1 BALLDONTLIE
- **`match_ids[]=X` 배열 형식 필수** (`match_id=X` 단수형 무시됨). `database.py` `MATCH_FILTER_KEY`.
- **Rate limit 동적 페이싱** (`x-ratelimit-remaining`≤1이면 reset까지 sleep).
- **2018 team-level xG NULL** → 휴리스틱 fallback.
- **`/players` 벌크 호출 금지** (30k+ 반환, burst limit). placeholder 등록 후 `/rosters`로 이름 채움.
- **API raw 응답의 type 필드는 `incident_type`** (nested player). **단, DB 적재 후 컬럼명은 `event_type`** — DB 쿼리는 `event_type` 사용.

### 11.2 PostgreSQL 호환 (database.py `_translate_sql`)
- `?`→`%s`, `%`→`%%`, `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`.
- **GROUP BY strict:** SELECT 비집계 컬럼 전부 GROUP BY에. (`GROUP BY t.id, t.name`)
- **HAVING은 SELECT alias 못 씀.**
- **집계 + ORDER BY 비집계는 서브쿼리로.**

### 11.3 Railway
- **`database.init_db()` 모듈 레벨 호출** (gunicorn 대응).
- **`*.db` gitignore** → Railway 첫 부팅 빈 DB. `migrate.py` 또는 백그라운드 sync로 채움.
- **`Procfile`: `web: gunicorn app:app --workers 1`** — **워커 1개 필수** (멀티워커 시 sync 스레드 중복 → rate-limit 초과).
- **에러 핸들러**가 traceback을 stderr+stdout으로 dump (`=== 500 ERROR ===` 마커) → Railway Logs. `DEBUG_TRACEBACK=1` 시 브라우저.
- railway CLI는 이 개발 환경에 없음 — 로그는 Railway 웹 대시보드 또는 운영자가 직접 `! railway logs`.

### 11.4 Polymarket
- `outcomePrices`는 JSON 문자열. 제목 앞 공백 `.strip()`. Golden Boot Winner 마켓 없음.

### 11.5 모델
- **`PatternMatcher` 분산=0 피처 → std=1e9 무시.**
- **2018 `was_upset`는 shots 기반** (xG 없음).
- **GroupSituationEngine 한계 (전부 docstring에 명시):** 페어플레이=총 카드수(옐로/레드 구분 데이터 없음), FIFA랭킹=정적 근사 dict, 잔여일정 시나리오는 대표 스코어(승 2-0/무 1-1/패 0-2)+동점 순위 범위화, 3위 cross-group 컷 미평가, 타이브레이커 1-3 재귀 재적용 미구현.
- **GroupSituation note는 영어** ("Qualification open", "Must win to survive", "Eliminated" 등). index.html 이모지 분기가 이 영어 문자열 키워드에 의존.

### 11.6 시간대 (US Eastern, DST 자동) — ✅ 결정됨 (2026-06-16)
- `app.py` **`EASTERN = ZoneInfo("America/New_York")`** (DST 자동). 여름 EDT(UTC-4)/겨울 EST. 이전 `EST=timezone(UTC-5)` 고정에서 전환 — 6~7월 월드컵 = 실제 EDT라 운영자가 America/New_York 선택.
- **표시(display)만 Eastern, 내부 로직·저장은 전부 UTC.** `_est_dt/_est_time/_est_label`이 변환 담당. `_run_due_predictions`의 킥오프 2h 전 비교는 **전부 tz-aware UTC** (DST 무관, 회귀 없음). predictions `created_at`도 UTC naive 저장.
- 라벨은 하드코딩 금지 — `_est_label()`=`tzname()`("EST"/"EDT") 사용. 카드는 `tz` 필드, 사이드바는 `est.tzname()`.
- **`tzdata` 의존성 필수** (Windows). 없으면 `ZoneInfoNotFoundError` 500.
- index "오늘"/사이드바/카드/match ctx 모두 Eastern. UTC date와 Eastern date가 자정 경계서 어긋날 수 있어 index는 UTC 쿼리창 ±1일 넓혀 Python에서 Eastern date로 필터.

### 11.7 프런트 렌더 (Railway에서 자주 터짐)
- **확률/스코어라인 바는 `<div>`(블록) + 인라인 `style="width:X%; background:#..."`로.** `<span>`(inline)에 width 주면 **안 먹혀 빈 바**. CSS 클래스만 의존 금지 (CSS 로드 실패/inline 한계). 색도 인라인으로 박을 것.
- **국기 이모지는 Twemoji 필수** (base.html). 없으면 Windows에서 "SE"/"TN"으로 깨짐. `_flag()`는 정상 이모지 반환(country_code 아님), 미지정 시 `""`.

### 11.8 PostgreSQL Decimal (★ Railway 500 주범이었음)
- **PG `AVG(정수컬럼)`은 `decimal.Decimal` 반환** → `Decimal * float` TypeError. (SQLite는 float라 로컬선 멀쩡 → 디버깅 함정)
- **DB에서 가져온 숫자는 전부 `float()` 캐스팅.** `_team_strengths_from_db`, `_player_xg_adjustment`, `predict_from_db`의 ha/hd/aa/ad 등에 적용됨. 새 DB 쿼리 추가 시 항상 float() 래핑.

### 11.9 Temperature 캘리브레이션 + 평가 아티팩트 + v1.1 strength/upset
- **`calibrate_wdl`은 라이브 compute 3곳에만, POST 스냅샷 경로엔 절대 적용 금지** — 과거 저장 예측 소급 변환되면 안 됨(컷오버 = `model_version="v1.1+tcal"`). 새 W/D/L 노출 경로 추가 시 calibrate 적용 + 스냅샷 경로 제외 일관성 유지.
- **단조변환이라 argmax 보존** — 캘리브레이션 바꿔도 predicted_outcome·SEASON RECORD 불변이어야 정상. 바뀌면 버그(중단).
- **★ strength shrinkage는 `_team_strengths_from_db` 단일 출처** (model+표시카드 동시 반영). n<3 팀은 attack/defense가 중립 1.0 방향 수축됨 — 라이브 카드가 raw와 다를 수 있음(정상, `attack_raw`/`defense_raw`도 반환). 새 strength 소비 경로는 이 dict를 그대로 쓸 것(재계산 금지).
- **★ λ cap = `[0.3, 5.0]`** (`_expected_goals`+`apply_user_notes` 둘 다). 한쪽만 고치면 notes가 λ를 다시 3.0으로 잘림 — 항상 같이.
- **★ upset_pct = ELO 약체 모델 승률** (표시 전용, 저장 안 함). PatternMatcher k-NN으로 되돌리지 말 것(elo_diff 무시 → 0% 고정 회귀). §6.3.
- **draw cap(0.35) 미적용 — 운영자 결정(2026-06-20).** 재도입 시 반드시 **argmax-safe**(draw가 argmax 아닐 때만, 초과분 home/away 비례 재분배)로, /methodology에 명기. 무지성 draw 억제는 §11.9 argmax 규칙 위반.
- **`static/calibration_report.json`이 /methodology의 진실원.** Railway엔 `results.csv`(martj42, 로컬 temp) 없어 **백테스트 재계산 불가** → JSON을 로컬에서 `--dump`로 만들어 **커밋**해야 라이브 반영. 코드/JSON 어긋남 방지 위해 meta에 `git_commit`/`generated_at` 박힘(페이지 footer 노출).
- **`backtest_calibration.py`는 라이브 엔진 `DixonColesEngine.predict()` 재사용**(새 엔진 X). 입력(strength/ELO)만 as-of international로 교체. `player_xG_adj`/`situation_mult`는 2026 전용이라 백테스트 미검증(페이지에 명기). scipy 금지 — golden-section.

### 11.10 Knockout 브래킷/진출 (2026-06-28)
- **sync 팀 교정 필수:** `sync_live_lite` UPDATE에 `home/away_team_id=COALESCE(?,기존)` — `upsert_matches`는 INSERT OR IGNORE라 기존 행을 절대 안 고침. 빼면 knockout이 "2A vs 2B" placeholder 고착(score/status만 갱신). team_id는 score/status와 **항상 같이** UPDATE.
- **브래킷 wiring 하드코딩(`app.py _KO_R16/_KO_QF/_KO_SF`):** BALLDONTLIE `match_number` 필드 깨짐(전부 6, 신뢰불가). 대신 **R32 kickoff순 = 공식 match# 73-88** 가정 + R16+ feeder는 placeholder "W##" source 라벨에서 복원. R32 일정/순서 바뀌면 wiring 재확인.
- **트로피 = `board[0]`(우승확률 1위), modal champ 아님.** modal 브래킷(매 경기 favorite 진출)의 최종 우승자와 MC 우승확률 1위는 전력 근소차 시 다를 수 있음(France 11.8 vs Brazil 10.8). 혼란 방지차 트로피는 항상 board[0].
- **진출확률 = `win + 0.5*draw` (calibrate 후).** match `adv1/adv2`와 `_ko_p_advance` 동일 공식. 연장/승부차기 모델링 안 함(coinflip) — 2018/2022 백테스트 Brier 0.2216으로 검증, 학술 ET×⅓ 시뮬과 동등.
- **knockout 판정 = `group_name is None`** (조별만 group 존재). `is_knockout`은 PRE 경로에서만 진출% 바로 전환, POST/group 무변경.

---

## 12. 진행 상태

완료:
- [x] database.py 11 tables + 듀얼 백엔드 + PG 호환
- [x] data_pipeline.py 백필 + 동적 페이싱 + **`sync_live_lite` + `enrich_completed_matches_2026`**
- [x] model.py 7 엔진 (+ DixonColes, GroupSituation) + FIFA 기반 ELO 시드 + MLE ρ
- [x] **DixonColes 웹 연결** — match/index 확률이 DixonColes 산출 (5단계 λ)
- [x] **백그라운드 sync 워커** (스레드) + 완료 시 enrich + ELO 재빌드 + 예측 저장
- [x] **POST-MATCH 페이지 + 예측 추적 (save_prediction/compute_brier_score/`/api/brier`/SEASON RECORD)**
- [x] **user_notes → 모델 λ 반영** (`apply_user_notes`, app.py `_load_user_notes`)
- [x] **MATCHIQ 리브랜드** (영어 UI, 사이드바, PRE/POST, 국기, EST, MODEL EDGE, 구조화 리뷰)
- [x] **edge 스냅샷** (predictions에 suggested_bet/draw_edge/total_xg) + POST "Pre-Match Odds/Signal" — **이후 Layer B에서 predicted_winner를 순수 argmax로 교체(`d0e69a1`), 26% baseline·비대칭 문턱 제거. draw_edge는 신규 NULL.**
- [x] **Layer B argmax 라벨링** (`d0e69a1`) — `generate_prediction_label`, `b["prediction"]`, 새 컬럼 predicted_outcome/confidence/is_tossup(비파괴 ADD), 중립 xG 표시. 검증 OLD 2/5→argmax 3/5.
- [x] **Temperature 캘리브레이션 T=1.718** (`ddac4a8`) — `calibrate_wdl` 후처리 3곳, argmax 보존, `v1+tcal` 컷오버. 누수 없는 백테스트 검증(ECE 0.083→0.026). §11.9.
- [x] **/methodology 평가 페이지** (`2b18097`) — `calibration_report.json` 기반 reliability SVG·지표표·한계, `backtest_calibration.py` 커밋(재현). 라이브 검증 완료.
- [x] **v1.1+tcal 5버그 수정** (`35bea2e`) — strength 경기수 shrinkage(BUG1/4), λ cap 3.0→5.0(BUG3), upset=ELO 약체 모델 승률(BUG5), draw 근본 해소(BUG2, cap 미적용). model_version 컷오버. 로컬+라이브 검증 완료. §3.1·§6.1·§6.3·§11.9.
- [x] **"No prediction recorded"** — 스냅샷 없는 완료 경기는 가짜 적중 판정 안 함 (`63455fd`)
- [x] **Eastern DST 자동** (`ZoneInfo America/New_York` + `tzdata` + 동적 라벨) — §11.6 결정 완료 (`63455fd`)
- [x] **POST "Pre-Match Odds" 스냅샷-only** — 재계산값 표시 차단 (`fc359f8`)
- [x] README.md (방법론/아키텍처) + 스크린샷 커밋
- [x] Railway 배포

미완 / 다음:
- [ ] **Railway PG에 실제 데이터 마이그레이션** (`migrate.py`) — 또는 백그라운드 sync가 채우게 (운영자 확인 필요). teams 112/matches 104는 적재됨.
- [x] **SEASON RECORD 자동 채워짐** — Railway predictions 6건(14–19), 4/6 CORRECT. 백그라운드 sync가 킥오프 전 자동저장→완료 전환→채점 (라이브 검증됨).
- [ ] **away-favorite richer calibration** — 단일 T 후 잔여 과신(n=7, gap+0.23). per-class/vector scaling 신호, 표본 더 모이면 검토(현재 미적용).
- [ ] **draw cap(argmax-safe 0.35) 재검토** — v1.1 후 압도적 mismatch 7경기(|gap|≥200)가 CAL draw 31~38% 잔존(전부 draw≠argmax). 운영자 결정으로 현재 미적용. §11.9.
- [ ] **README 스크린샷 재촬영**(선택) — Eastern/odds/calibration/methodology 반영.
- [ ] LIVE phase 실시간 푸시 (현재 60s DB 폴링 + 백그라운드 sync)
- [ ] Monte Carlo 토너먼트 진출 시뮬레이션 (3위 cross-group 컷 포함)
- [ ] 모바일 QA + style.css 폴리시 (새 클래스 일부 CSS 없이 인라인만 적용된 곳 있음)
- [ ] model.py의 미표시 엔진(Tactical/Player/Narrative) 한국어 → 영어 정리 (UI 미노출이라 보류 중)

---

## 13. 작업 규칙

1. **단순화 우선.** 요청한 것만. 추측성 추상화 금지.
2. **외과적 변경.** 무관한 인접 코드 손대지 말 것.
3. **검증 가능한 목표.** "어떻게 확인할지" 명시 (test_client/렌더 검증).
4. **할루시네이션 금지.** 검증 안 된 API/응답/테이블 단언 금지.
5. **실거래 영향.** 확률에 무성의 디폴트 금지. 데이터 결손 시 명시.
6. **언어.** 운영자 대화 한국어. **웹 UI·코드·커밋·주석·로그 영어.**
7. **파괴적 명령 사전 확인.** rm/force-push/hard reset/DB drop 등.

---

## 14. GitHub

- Remote: `https://github.com/Gunnerista/worldcup-predictor.git`, branch `main`
- 커밋 메시지 영어. **민감정보 git 유출 없음 확인됨** (.env/*.db gitignore, 히스토리·추적파일·스크린샷 전부 클린 — 2026-06-15 점검).
- 최근 마일스톤:
  - `fix(model): shrinkage, xG cap, upset prob, attack floor — v1.1+tcal` (35bea2e)
  - `feat: /methodology evaluation & calibration page (data-driven)` (2b18097)
  - `feat: temperature-calibrate W/D/L distribution (T=1.718, argmax-preserving)` (ddac4a8)
  - `Layer B: replace draw-biased threshold cascade with argmax labeling` (d0e69a1)
  - `fix: POST-MATCH Pre-Match Odds shows snapshot only, never recompute` (fc359f8)
  - `feat: show "No prediction recorded" + DST-aware Eastern time` (63455fd)
  - `feat: MATCHIQ rebrand — full English, team names replace home/away, date sidebar, pre/post modes`
  - `feat: wire DixonColes + GroupSituation + user notes to web UI`
  - `feat: prediction tracker (auto pre-kickoff save + Brier calibration)`
  - `fix: cast Decimal to float for PostgreSQL compatibility`
  - `feat: replace arbitrary ELO seeds with FIFA ranking points (2026 pre-tournament)`
  - `feat: MLE rho estimation from 2018/2022 World Cup data`
