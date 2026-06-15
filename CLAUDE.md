# CLAUDE.md — worldcup-predictor

이 파일은 프로젝트 단일 진실 공급원(SSoT). 컨텍스트가 꽉 차서 새 세션을 열어도, 이 문서만 읽으면 작업을 그대로 이어갈 수 있어야 함.

---

## 1. 프로젝트 정체성 (Identity)

- **이름:** worldcup-predictor
- **목표:** FIFA World Cup 2026 경기 승/무/패 확률을 수학적으로 계산해서 컨설팅 리포트 형태로 보여주는 웹사이트.
- **용도:** Polymarket / Kalshi에서 사용자가 **직접** 베팅 결정을 내리기 위한 의사결정 보조 도구.
- **자동 베팅 절대 금지.** 코드 어디에도 거래소 주문 API를 호출하는 로직을 넣지 말 것.
- **컨셉:** "축구도사" — 단순 확률 계산기가 아니라, **왜 이 확률인지 근거까지 설명**하는 분석가 수준의 리포트.

---

## 2. 운영자 컨텍스트 (Operator)

- 운영자는 **비코더**. 코드 라인 단위로 검수 못 함.
- 따라서 모든 변경은:
  - 사용자 눈에 보이는 결과 위주로 한국어로 설명.
  - 가정·외부 API 동작은 반드시 검증 후 단언.
  - "아마 될 거예요" 금지. 실거래/실머니에 영향 갈 수 있는 코드는 무조건 테스트.
- 글로벌 규칙은 `~/.claude/CLAUDE.md` (Part A) 참고. 본 프로젝트 규칙이 충돌 시 본 파일이 우선.

---

## 3. 기술 스택 (Tech Stack)

| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.14 | Windows 11 환경 |
| 웹 프레임워크 | Flask | 가벼움, SSE 친화적 |
| 실시간 푸시 | **Server-Sent Events (SSE)** | WebSocket 아님 |
| DB | **SQLite** | PostgreSQL 아님 |
| 프런트 | HTML / CSS / Vanilla JS | 별도 프레임워크 없음 |
| 디자인 | ESPN / Opta 스타일, 다크 배경 `#0d1117`, 포인트 형광 초록 + 파랑 |
| 모바일 | 필수 대응 (mobile-first) |

---

## 4. 데이터 소스 (Data Sources)

### 4.1 BALLDONTLIE FIFA API
- **플랜:** GOAT $39.99/월 (분당 600 요청)
- **베이스 URL:** `https://api.balldontlie.io/fifa/worldcup/v1/`
- **API 키:** `.env`의 `BALLDONTLIE_API_KEY` (절대 커밋 금지)
- **사용 엔드포인트:**
  - `teams`, `players`, `rosters`
  - `matches`, `match_events`
  - `player_match_stats`, `team_match_stats`
  - `match_shots`, `match_momentum`
  - `match_best_players`, `match_avg_positions`, `match_team_form`
  - `group_standings`
  - `odds`, `player_props`, `futures`
- **특성:**
  - 경기 중 데이터 실시간 자동 업데이트 (골 발생 즉시 반영)
  - 2018 / 2022 / 2026 대회 데이터 모두 포함 (`seasons[]` 파라미터)

### 4.2 Polymarket Gamma API
- **URL:** `https://gamma-api.polymarket.com`
- **인증:** 없음 (무료, API 키 불필요)
- **용도:** 월드컵 관련 마켓 현재 배당 조회 → 내 모델 확률과 비교 → 가치 베팅(value bet) 기회 식별

---

## 5. 예측 모델 (Model)

### 5.1 알고리즘
- **ELO 레이팅 → XGBoost 분류기 → Platt Scaling**
  - ELO로 베이스라인 강도 차이.
  - XGBoost가 비선형 피처(폼, xG, 모멘텀 등)를 학습.
  - Platt scaling으로 출력 확률을 캘리브레이션(보정).

### 5.2 입력 피처 (Features)
- 팀 폼: 이번 대회 + 2022 데이터
- 선수 개별 xG 합산
- 슈팅 위치 / 정확도
- 맞대결 기록 (2022 / 2026)
- 팀 평균 점유율
- 누적 피로도 (출전 시간)
- 공격 모멘텀 (Match Momentum API)

### 5.3 사용자 주관 입력 (Subjective Overrides)
최종 확률 산출에 다음 메모를 가중치로 반영:
- 감독 전술 변화 메모
- 선수 컨디션 특이사항
- 심리적 요소
- 기타 메모

→ DB에 `user_notes` 테이블로 저장하고, 모델 후처리 단계에서 확률 조정.

---

## 6. 웹사이트 구조 (3 Phases)

### 6.1 PRE-MATCH (경기 전)
- 승 / 무 / 패 확률 % (내 모델 결과)
- **왜 이 확률인지 근거 3가지 자동 생성**
  - 예) "A팀은 B팀보다 평균 xG 0.8 높음"
  - 예) "최근 3경기 무실점 수비"
  - 예) "2022년 맞대결 2-0 승리"
- 16강 / 8강 / 4강 / 우승 진출 확률 (Monte Carlo 시뮬레이션)
- 선수 TOP3 (xG 기준)
- 팀 폼 시각화 (최근 경기 결과)
- **Polymarket 현재 배당 vs 내 모델 비교** — 가치 베팅 기회 하이라이트
- 내 메모 입력 필드 (감독 / 컨디션 / 심리 / 기타)

### 6.2 LIVE (경기 중)
- 60초 폴링 + 골 / 카드 이벤트 발생 시 **즉시 확률 재계산**
- **SSE로 브라우저에 실시간 푸시** (WebSocket 아님)
- 현재 스코어 + 경과 시간
- 공격 모멘텀 그래프 (Match Momentum API 직접 사용)
- 누적 xG, 슈팅 수, 점유율 실시간 업데이트
- 상황별 자동 코멘트 (데이터 기반)
  - 예) "75분 이후 A팀 xG 급등 — 역전 시나리오 가능성 상승"

### 6.3 POST-MATCH (경기 후)
- 전체 통합 리포트
- 내 예측 확률 vs 실제 결과 비교 (캘리브레이션 추적)
- Polymarket 배당 vs 내 모델 비교 (사후)
- 다음 경기 미리보기

---

## 7. 폴더 구조 (File Tree)

```
worldcup-predictor/
├── app.py                # Flask 엔트리, 라우트 + SSE 스트림
├── model.py              # ELO + XGBoost + Platt scaling
├── data_pipeline.py      # BALLDONTLIE 클라이언트 + ingestion
├── polymarket.py         # Polymarket Gamma 클라이언트
├── database.py           # SQLite 스키마 + 헬퍼
├── templates/
│   ├── index.html        # 경기 목록 / 랜딩
│   └── match.html        # 단일 경기 (pre / live / post 통합)
├── static/
│   ├── style.css         # 다크 테마, 모바일 대응
│   └── charts.js         # 차트 + SSE 클라이언트
├── .env                  # 비밀키 (커밋 금지)
├── .gitignore
├── CLAUDE.md             # ← 이 파일
└── requirements.txt
```

---

## 8. 의존성 (requirements.txt)

```
flask
flask-cors
python-dotenv
requests
xgboost
scikit-learn
numpy
pandas
```

---

## 9. 환경 변수 (.env)

`.env` 템플릿:
```
BALLDONTLIE_API_KEY=your_key_here
FLASK_SECRET_KEY=your_secret_here
FLASK_DEBUG=True
```

- 실제 키 값은 운영자가 직접 입력.
- **`.env`는 `.gitignore`에 포함되어 있음 — 절대 커밋 금지.**

---

## 10. 작업 규칙 (Working Rules)

1. **단순화 우선:** 요청한 것만 구현. 추측성 기능·추상화·"유연성" 금지.
2. **외과적 변경:** 사용자 요청과 무관한 인접 코드 손대지 말 것.
3. **검증 가능한 목표:** 모든 작업은 "어떻게 확인할지" 명시.
4. **할루시네이션 금지:** 검증 안 된 API 시그니처 / 엔드포인트 / 응답 구조 단언 금지. `WebFetch` 또는 공식 문서로 확인 후 코드 작성.
5. **실거래 영향:** 베팅 의사결정 보조 도구이므로, 확률 계산·표시에 무성의한 디폴트 값 금지. 데이터 결손 시 사용자에게 명시.
6. **언어:** 사용자 대화는 한국어. 코드 / 커밋 / 주석 / 로그는 영어.
7. **파괴적 명령 사전 확인:** `rm`, `git push --force`, `git reset --hard`, DB drop / truncate 등은 반드시 사용자 승인 후 실행.

---

## 11. 진행 상태 (Progress Tracker)

다음 세션이 어디서 이어야 하는지 확인하는 곳. 단계 끝낼 때마다 체크.

- [x] Step 1 — 폴더 구조 + 빈 파일 생성
- [x] Step 2 — CLAUDE.md 작성 (이 파일)
- [x] Step 3 — `requirements.txt`
- [x] Step 4 — `.env` 템플릿
- [x] Step 5 — `.gitignore`
- [x] Step 6 — GitHub 초기 커밋 + push (remote: `https://github.com/Gunnerista/worldcup-predictor.git`)
- [x] Step 7 — `database.py` 스키마 정의 (11 tables, 6 indexes, FK enabled)
- [ ] Step 8 — `data_pipeline.py` BALLDONTLIE 클라이언트 + 백필 (스모크 테스트만 완료)
- [ ] Step 9 — `model.py` ELO 베이스라인 + XGBoost + Platt
- [x] Step 10 — `polymarket.py` 토너먼트 마켓 조회 (조 우승 / 16강·8강·4강 진출 / 시상 / props)
- [ ] Step 11 — `app.py` 라우트 + SSE
- [ ] Step 12 — `templates/` + `static/` UI 구현
- [ ] Step 13 — 사용자 메모 입력 → 모델 후처리 반영
- [ ] Step 14 — Monte Carlo 토너먼트 시뮬레이션
- [ ] Step 15 — 모바일 QA + 캘리브레이션 추적

---

## 12. GitHub

- **Remote:** `https://github.com/Gunnerista/worldcup-predictor.git`
- **기본 브랜치:** `main`
- **커밋 메시지 언어:** 영어
