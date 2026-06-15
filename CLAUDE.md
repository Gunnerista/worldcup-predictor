# CLAUDE.md — worldcup-predictor

이 파일은 프로젝트 단일 진실 공급원(SSoT). 컨텍스트가 꽉 차서 새 세션을 열어도, 이 문서만 읽으면 그대로 이어갈 수 있어야 함.

> **새 세션 필독:** §11 함정(Gotchas) 먼저 읽어. 같은 함정에 두 번 빠지지 마.

---

## 1. 프로젝트 정체성

- **이름:** worldcup-predictor
- **목표:** FIFA World Cup 2026 경기 승/무/패 확률을 수학적으로 계산해서 컨설팅 리포트 형태로 보여주는 웹사이트.
- **용도:** Polymarket / Kalshi에서 사용자가 **직접** 베팅 결정을 내리는 의사결정 보조 도구.
- **자동 베팅 절대 금지.** 코드 어디에도 거래소 주문 API 호출 로직 넣지 말 것.
- **컨셉:** "축구도사" — 단순 확률 계산기가 아니라 **왜 이 확률인지 근거까지 설명**하는 분석가 수준 리포트.

---

## 2. 운영자 컨텍스트

- 운영자는 **비코더**. 코드 라인 단위 검수 못 함.
- 모든 변경은:
  - 사용자 눈에 보이는 결과 위주로 한국어 설명.
  - 외부 API 동작은 검증 후 단언. "아마 될 거예요" 금지.
  - 실거래/실머니 영향 코드는 무조건 테스트.
- 글로벌 규칙은 `~/.claude/CLAUDE.md` 참고. 충돌 시 본 파일 우선.

---

## 3. 현재 상태 (Live)

- **GitHub:** `https://github.com/Gunnerista/worldcup-predictor.git` (branch: `main`)
- **배포:** Railway (web 서비스 + PostgreSQL 서비스)
- **로컬 DB:** `worldcup.db` (SQLite) — 128경기 풀 백필 완료, gitignored
- **Railway DB:** PostgreSQL — 스키마 자동 생성됨, 데이터는 `migrate.py`로 옮겨야 채워짐

---

## 4. 기술 스택 (실제)

| 영역 | 선택 |
|---|---|
| 언어 | Python 3.14 (Windows 11 로컬, Railway는 Python 3.13) |
| 웹 프레임워크 | Flask 3.x |
| WSGI | gunicorn (Railway), `python app.py` (로컬) |
| 실시간 푸시 | Server-Sent Events (SSE) — WebSocket 아님 |
| DB | **SQLite (로컬) + PostgreSQL (Railway)** — `database.py`가 듀얼 백엔드 |
| 프런트 | HTML / CSS / Vanilla JS — 별도 프레임워크 없음 |
| 차트 | Chart.js CDN (LIVE 페이지만) |
| 디자인 | ESPN/Opta 스타일, 다크 `#0d1117`, 형광 초록 `#00ff88` + 파랑 `#58a6ff` |
| 모바일 | 필수 (`@media max-width: 720px`) |

---

## 5. 데이터 소스

### 5.1 BALLDONTLIE FIFA API
- **베이스 URL:** `https://api.balldontlie.io/fifa/worldcup/v1/`
- **API 키:** `.env` → `BALLDONTLIE_API_KEY`
- **⚠️ 실제 rate limit: 분당 5 요청** (헤더 `x-ratelimit-limit: 5`로 확인됨). 운영자 키가 GOAT 플랜(600/분)이 아닌 것으로 추정. `data_pipeline.py` `_get()`이 응답 헤더 `x-ratelimit-remaining`/`reset`을 읽어 동적 페이싱.
- **사용 엔드포인트:** teams, matches, team_match_stats, player_match_stats, match_shots, match_momentum, match_events, rosters
- **2018 데이터엔 team-level xG 없음** (2022는 있음). `match_shots`의 `xg`/`xgot`는 양 시즌 모두 있음.

### 5.2 Polymarket Gamma API
- **URL:** `https://gamma-api.polymarket.com`
- **인증:** 없음 (무료)
- **⚠️ 경기별 W/D/L 마켓 없음.** 토너먼트 단위만 존재 (조 우승, 16강/8강/4강 진출, Golden Boot/Ball/Glove, props).
- **올바른 엔드포인트:** `/events?tag_slug=world-cup` (NOT `/markets?tag=soccer` — 그건 필터 무시되고 디폴트 페이지 반환).

---

## 6. 예측 모델 (실제 아키텍처)

**원래 계획이었던 XGBoost + Platt scaling 안 씀.** 순수 Python 5 엔진 (전부 `model.py`):

| 엔진 | 역할 |
|---|---|
| **EloEngine** | 표준 ELO + 무승부 모델 (`_DRAW_SIGMA=350`) + 2026 호스트(+50)/CONMEBOL 근접(+20) 보너스 |
| **TacticalEngine** | 약점 zone 분석, 점유율 영향(슬로프 10%p당 5%p), 고지대 보정 (Estadio Azteca 등), 조별리그 상황 모티베이션 |
| **PlayerMatchupEngine** | 임팩트 스코어 (xG×30 + pass_rate×20 + recov×1.5 + key_passes×4), 휴식일 피로도, 월드컵 동기부여 |
| **PatternMatcher** | z-score 정규화 유클리드 거리 → 유사도%. 분산=0 피처는 1e9 std로 자동 무시. 레드카드 영향(분 단위 swing) |
| **NarrativeEngine** | 다른 엔진 출력만 받아 한국어 리포트 조립. API echo 절대 금지 — 모든 숫자는 엔진 계산 결과 |

**ELO 시드:** `app.py`의 `INITIAL_RATINGS_2026` dict — Tier 1 (1700-1780) ~ Tier 4 (1310-1410). 그 위에 2018+2022 결과 replay. (1500부터 시작 안 함 — Spain 1500에서 시작하면 절대 1700 못 도달.)

---

## 7. 웹사이트 구조 (PRE / LIVE / POST)

### PRE-MATCH (현재 주력)
승/무/패 % → 이전 경기 복기 → 이번 경기 핵심 → 모델 근거 3가지 → TOP 3 선수 → 유사 과거 경기 → 경고 시그널 → 월드컵 특수 요소 → Polymarket 배당 → 내 메모 입력.

### LIVE (스켈레톤만)
- 60초 폴링 (`/stream/<id>` SSE)
- 라이브 데이터 푸시 워커는 아직 없음 — 매 60s DB 폴링만
- Chart.js 모멘텀/점유율 차트 (`data-phase="live"`일 때만 활성)

### POST-MATCH
현재 `/match/<id>`로 리다이렉트만. 별도 페이지 미구현.

---

## 8. 파일 구조 (실제)

```
worldcup-predictor/
├── app.py                # Flask 라우트 + 엔진 워밍 + SSE
├── model.py              # 5개 엔진 + DB historical loader + INITIAL_RATINGS_2026
├── database.py           # 듀얼 백엔드 + PostgreSQL 호환 레이어
├── data_pipeline.py      # BALLDONTLIE 백필 (CLI: backfill/sync/today/live/names/events)
├── polymarket.py         # Polymarket Gamma 클라이언트
├── migrate.py            # SQLite → Railway PG 일회성 데이터 마이그레이션
├── Procfile              # web: gunicorn app:app --workers 1
├── requirements.txt
├── templates/
│   ├── index.html        # 오늘 경기 카드 목록
│   └── match.html        # PRE/LIVE/POST 통합
├── static/
│   ├── style.css         # ESPN/Opta 다크 테마
│   └── charts.js         # Chart.js + SSE 클라이언트
├── .env                  # 로컬 비밀키 (gitignored)
├── .gitignore            # .env, *.db, __pycache__/, .gstack/
├── CLAUDE.md             # ← 이 파일
└── worldcup.db           # 로컬 SQLite (gitignored, 128경기 백필됨)
```

---

## 9. 의존성 (실제 requirements.txt)

```
flask
python-dotenv
requests
gunicorn
psycopg2-binary
```

**원래 계획에 있던 xgboost / scikit-learn / numpy / pandas / flask-cors는 한 줄도 import 안 함 — 제거됨.** Railway 빌드 시간/메모리 부담 줄임.

---

## 10. 환경 변수

### 로컬 `.env`
```
BALLDONTLIE_API_KEY=실제키
FLASK_SECRET_KEY=...
FLASK_DEBUG=True
```

### Railway Variables
- `BALLDONTLIE_API_KEY` — 필수 (오늘 경기 / 매치 디테일 API 호출)
- `DATABASE_URL` — Railway Postgres 서비스 attach 시 자동 주입. `database.py`가 감지해서 PostgreSQL 경로 선택
- `PORT` — Railway 자동 주입. gunicorn이 사용
- `DEBUG_TRACEBACK` — 진단용 토글. 켜져 있으면 에러 발생 시 풀 트레이스백을 브라우저에 표시 (운영 중엔 반드시 꺼야 함, 보안 위험)

---

## 11. 함정 (Gotchas) — **새 세션 필독**

### 11.1 BALLDONTLIE
- **`match_ids[]=X` 배열 형식 필수.** `match_id=X` 단수형은 silently ignored되어 디폴트 페이지 반환. `database.py`의 `MATCH_FILTER_KEY = "match_ids[]"` 강제.
- **Rate limit dynamic pacing.** 응답 헤더의 `x-ratelimit-remaining`이 ≤1이면 `x-ratelimit-reset`까지 sleep. 정적 throttle만 쓰면 429 풀잎.
- **2018 데이터엔 team-level `expected_goals` NULL.** 패턴 매칭 휴리스틱이 xG 없을 때 shots_diff로 fallback.
- **`/players` 페이지네이션은 BALLDONTLIE 전체 DB 30k+ 명을 반환** → burst limit 강제 트리거. `/players` 벌크 호출 금지. 대신 백필 중 player_id 등장 시 placeholder로 등록하고 나중에 `/rosters?team_ids[]=X`로 이름 채움.
- **`match_events`의 type 필드는 `incident_type`** (NOT `event_type` 또는 `type`). 또 `player_id`가 nested (`{"player": {"id":...}}`).

### 11.2 PostgreSQL 호환 (database.py `_translate_sql`)
호환 레이어가 자동 처리하지만 이해는 해둘 것:
- **`?` → `%s`** (psycopg2 paramstyle)
- **`%` → `%%`** (psycopg2가 `%`를 format 문자로 인식 → 리터럴 `%` 보호. LIKE 패턴에서 특히 중요)
- **`INSERT OR IGNORE INTO ...` → `INSERT INTO ... ON CONFLICT DO NOTHING`**
- **GROUP BY strict:** SELECT의 비집계 컬럼은 GROUP BY에 다 들어가야 함. `GROUP BY p.id`만 있고 `p.name`을 SELECT하면 에러.
- **HAVING은 SELECT alias 못 씀.** `HAVING games >= 1` 안 됨, `HAVING COUNT(*) >= 1` 또는 제거.
- **집계함수 + ORDER BY 비집계 컬럼은 서브쿼리로 감싸야 함.** (예: `_recent_team_stats`가 "최근 3경기 평균" 하려면 ORDER BY + LIMIT를 inner SELECT로)

### 11.3 Railway
- **`database.init_db()`는 모듈 레벨에서 호출되어야 함.** `if __name__ == "__main__":` 안에 두면 gunicorn에선 안 돌아감 → 첫 요청에서 "no such table" 크래시.
- **`.gitignore`의 `*.db`가 worldcup.db를 제외.** Railway 첫 부팅 시 빈 DB. 데이터 옮기려면 `python migrate.py` (로컬에서 실행, DATABASE_URL은 Railway PG 공용 URL).
- **`Procfile`은 `web: gunicorn app:app --workers 1`** — Railway Nixpacks가 자동으로 `--bind 0.0.0.0:$PORT` 처리.
- **gunicorn 워커 1개 필수 (`--workers 1`) — 멀티워커 시 sync 스레드 중복 실행으로 rate-limit 즉시 초과.**
- **에러 핸들러 `@app.errorhandler(Exception)`** 가 traceback을 stderr로 dump → Railway Logs에 표시. `DEBUG_TRACEBACK=1` 시 브라우저에도 표시.

### 11.4 Polymarket
- **`outcomePrices`는 JSON 문자열** (NOT 리스트). `json.loads()` 필요.
- **이벤트 제목에 앞쪽 공백 있는 경우 종종 있음** (예: `" World Cup: Messi to Score a Free Kick?"`). 디스플레이 전에 `.strip()`.
- **Golden Boot Winner 마켓은 안 만들어져 있음** (2026 시점). Bronze/Silver Boot만 존재.

### 11.5 모델
- **`PatternMatcher` 분산=0 피처 처리:** 과거 매치가 elo_diff=0으로만 채워져 있는데 현재 입력은 큰 elo_diff면, std=1 fallback으로 그 피처가 distance 폭주시킴. → `std = 1e9` (무시)로 처리.
- **2018 매치는 `was_upset` 휴리스틱이 shots 기반** (xG 없으므로). `model.py` `_load_historical_from_db` 참고.

---

## 12. 진행 상태

- [x] 폴더 구조 + 초기 GitHub setup
- [x] `database.py` 11 tables + 듀얼 백엔드 + PG 호환 레이어
- [x] `data_pipeline.py` BALLDONTLIE 백필 (CLI 5종) + dynamic rate-limit pacing
- [x] `model.py` 5 엔진 (ELO/Tactical/Player/Pattern/Narrative) + INITIAL_RATINGS_2026 베이스라인
- [x] `polymarket.py` 토너먼트 마켓 클라이언트
- [x] `app.py` Flask 라우트 + 엔진 워밍 + SSE 스켈레톤 + 에러 핸들러
- [x] `templates/` + `static/` ESPN/Opta UI (PRE-MATCH 풀, LIVE 차트 준비)
- [x] Railway 배포 (gunicorn + PostgreSQL)
- [x] `migrate.py` SQLite → Railway PG 일회성 스크립트
- [ ] **Railway PG에 실제 데이터 마이그레이션** (`migrate.py` 실행) — 운영자가 직접 해야 함
- [ ] LIVE phase 실시간 데이터 푸시 워커 (현재는 60초 DB 폴링만)
- [ ] POST-MATCH 전용 페이지 + 모델 캘리브레이션 추적 (브라이어 스코어 등)
- [ ] 사용자 메모(`user_notes`) → 모델 확률 후처리 반영
- [ ] Monte Carlo 토너먼트 진출 확률 시뮬레이션
- [ ] 모바일 QA + 디자인 폴리시

---

## 13. 작업 규칙

1. **단순화 우선.** 요청한 것만 구현. 추측성 기능·추상화 금지.
2. **외과적 변경.** 요청과 무관한 인접 코드 손대지 말 것.
3. **검증 가능한 목표.** 모든 작업은 "어떻게 확인할지" 명시.
4. **할루시네이션 금지.** 검증 안 된 API/엔드포인트/응답 단언 금지.
5. **실거래 영향.** 확률 계산·표시에 무성의한 디폴트 금지. 데이터 결손 시 명시.
6. **언어.** 사용자 대화 한국어. 코드/커밋/주석/로그 영어.
7. **파괴적 명령 사전 확인.** `rm`, force-push, hard reset, DB drop 등은 운영자 승인 후.

---

## 14. GitHub

- Remote: `https://github.com/Gunnerista/worldcup-predictor.git`
- Branch: `main`
- 커밋 메시지: 영어
- 최근 큰 마일스톤 커밋 (참고용):
  - `Add complete web interface: ESPN-style PRE/LIVE/POST reports`
  - `Add PostgreSQL support for Railway deployment`
  - `Fix psycopg2 literal % escape in LIKE patterns`
  - `Add SQLite -> Railway PostgreSQL migration script`
