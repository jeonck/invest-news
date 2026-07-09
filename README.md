# 투자 뉴스 인사이트

매일 아침(KST 07:00) RSS · Reddit · Hacker News에서 국내외 투자 뉴스를 수집하고,
Claude가 [context.md](context.md)(내 투자 상황/관심 종목) 기준으로 **행동 판정**을 내린 뒤,
무관 판정을 제외한 항목만 Hugo 포스트로 커밋하여 GitHub Pages에 배포한다.

**사이트:** https://jeonck.github.io/invest-news/

## 판정 체계

| 판정 | 의미 | 예 |
|---|---|---|
| 🔥 즉시조치 | 보유·관심 종목(빅테크/AI)에 직접 영향을 주는 실적·규제 이슈, 또는 CPI/FOMC 등 포트폴리오 조정을 검토할 거시 이벤트 | 엔비디아 실적 가이던스 하향, 연준 금리 동결/인하 발표 |
| 📌 백로그 | 관심 종목/섹터에 해당하지만 급하지 않은 장기 트렌드 | AI 데이터센터 투자 확대 기사 |
| 📚 학습 | 직접 보유하진 않으나 거시경제·빅테크·증시 전반의 학습 가치 | 인플레이션 구조 분석, 반도체 업황 전망 |
| 무관 | 그 외 전부 → **포스트 생성 안 함** | 테마주 리딩, 클릭베이트성 시황 요약 |

각 포스트는 `근거`(내 포트폴리오/관심사 어디에 해당하는지)와 `액션`(검토·리밸런싱 등 구체적 작업 1개)을 담는다.

## 구조

```
.
├── context.md                  # 내 투자 상황/관심 종목 = 판정 기준 (여기를 고치면 판정이 바뀜)
├── feeds.yaml                  # 수집 소스 정의 (RSS/Reddit/HN)
├── pipeline/
│   ├── collect.py              # 수집 → 판정 → 포스트 생성
│   ├── requirements.txt
│   ├── processed.json          # 처리한 URL 해시 기록 (중복 방지, 90일 보존)
│   └── done.sh                 # 주간 리뷰: status 대기 → 완료
├── content/insights/           # 생성된 포스트
├── layouts/                    # 자체 Hugo 레이아웃 (외부 테마 없음)
├── hugo.toml                   # taxonomy: verdict / status / tags
└── .github/workflows/daily.yml # cron 수집 + Pages 배포
```

## 최초 세팅

1. **판정 인증 등록** — 둘 중 하나 (repo Settings → Secrets and variables → Actions)
   - **권장: Claude 구독 (Pro/Max)** — 로컬에서 `claude setup-token` 실행 → 브라우저 인증 →
     출력된 토큰을 `CLAUDE_CODE_OAUTH_TOKEN` Secret으로 등록.
     API 크레딧 불필요, 구독 사용량으로 차감됨.
   - **대안: Claude API 키** — `ANTHROPIC_API_KEY` Secret 등록 (계정에 크레딧 필요).
     `CLAUDE_CODE_OAUTH_TOKEN`이 없을 때만 사용됨.
2. **Pages 활성화** — Settings → Pages → Source: **GitHub Actions**
3. **첫 실행** — Actions 탭 → `Daily Insights` → Run workflow (수동 실행은 신규 항목이 없어도 배포함)

이후 매일 UTC 22:00 (KST 07:00) 자동 실행.

## 로컬 실행

```bash
pip install -r pipeline/requirements.txt

# claude CLI가 로그인돼 있으면 그대로 동작 (구독 인증, API 키 불필요)
# dry-run: 파일 생성/기록 갱신 없이 판정 결과만 stdout 출력
python pipeline/collect.py --dry-run

# 판정 건수 제한 (기본 30, 비용 안전장치)
MAX_ITEMS=5 python pipeline/collect.py --dry-run

# 실제 생성 후 로컬 미리보기
python pipeline/collect.py
hugo server        # → http://localhost:1313/invest-news/
```

환경변수:
- `JUDGE_BACKEND`: `claude-code`(구독, 기본 — claude CLI가 PATH에 있을 때) | `api`(API 키 과금)
- `CLAUDE_MODEL`(기본 `claude-sonnet-4-6`), `MAX_ITEMS`(기본 30), `GITHUB_TOKEN`(선택, rate limit 완화)

## 운영 루틴

**매일 아침 (2분)**
1. 사이트 접속 → 🔥 즉시조치 · 대기 확인 → 있으면 매수/매도/비중조절 등 액션 검토
2. 📌 백로그는 눈으로만 훑기

**매주 금요일 (15분)**
1. 백로그/학습 중 검토한 항목 완료 처리:
   ```bash
   ./pipeline/done.sh content/insights/2026-07-09-some-post.md
   git commit -am "review: weekly done" && git push
   # push 시 배포 전용 워크플로가 돌아 1~2분 내 사이트 반영됨
   ```
2. 판정 품질이 어긋나면 **context.md를 수정** (관심 종목 변경, 관심 분야 추가/삭제, 명시적 제외 보강)
   — 다음 실행부터 반영됨
3. 소스가 노이즈만 내면 feeds.yaml에서 제거

## 비용

- 항목당 1회 Claude 호출 (입력 ~2K 토큰 · 출력 ~200 토큰), 실행당 최대 `MAX_ITEMS`(30)건
- **claude-code 백엔드(기본)**: 별도 과금 없음 — Claude 구독(Pro/Max) 사용량으로 차감.
  판정 1건당 ~10초, 30건 ≈ 5분
- **api 백엔드**: context.md prompt cache 적용, 일 30건 × sonnet 기준 ≈ $0.05~0.1/일

## 알려진 제약

- **Reddit**: GitHub Actions 등 클라우드 IP에서 `.json` API가 403으로 차단되는 경우가 많음.
  실패해도 다른 소스는 정상 수집됨 (소스별 오류 격리).
- **hnrss.org**: 간헐적 502 — 해당 실행만 0건, 다음 실행에서 자동 회복.
- **크레딧 부족/인증 오류**: 즉시 중단하고 워크플로를 실패로 표시함 (의도된 동작 —
  Actions 실패 알림으로 인지). 인증 복구 후 다음 실행에서 미처리 항목 자동 재시도.
- **OAuth 토큰 만료**: `claude setup-token`으로 재발급 후 Secret 갱신.

## 투자 판단에 대한 주의

이 파이프라인은 뉴스 수집·1차 필터링 도구이며, 매수/매도를 대신 결정하지 않는다.
"즉시조치" 판정은 검토가 필요하다는 신호일 뿐 투자 조언이 아니다. 최종 판단과 책임은 본인에게 있다.
