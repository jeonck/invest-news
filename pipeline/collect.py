#!/usr/bin/env python3
"""Daily insight pipeline.

RSS/Reddit/HN/GitHub에서 새 글을 수집하고, Claude API로 context.md 기준
"행동 판정"을 내린 뒤, 무관이 아닌 항목만 Hugo 포스트로 생성한다.

Usage:
    python pipeline/collect.py [--dry-run]

Env:
    JUDGE_BACKEND            "claude-code" | "api" (기본: 자동 — claude CLI가 있으면
                             claude-code, 없으면 api)
    CLAUDE_CODE_OAUTH_TOKEN  claude-code 백엔드 CI 인증 (claude setup-token으로 발급,
                             로컬은 claude 로그인 세션 사용)
    ANTHROPIC_API_KEY        api 백엔드 필수
    CLAUDE_MODEL             판정 모델 (기본 claude-sonnet-4-6)
    MAX_ITEMS                1회 실행당 판정 최대 건수 (기본 30)
    GITHUB_TOKEN             선택 — GitHub Search API rate limit 완화
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.yaml"
CONTEXT_FILE = ROOT / "context.md"
PROCESSED_FILE = ROOT / "pipeline" / "processed.json"
CONTENT_DIR = ROOT / "content" / "insights"

USER_AGENT = "insight-pipeline/1.0"
FRESH_HOURS = 48          # RSS: 최근 48시간 항목만
PROCESSED_TTL_DAYS = 90   # 처리 기록 보존 기간
SUMMARY_MAX_CHARS = 1500  # 판정 프롬프트에 넣는 본문 상한

VERDICTS = ("즉시조치", "백로그", "학습", "무관")

JUDGE_PROMPT = """아래 항목을 읽고 반드시 다음 JSON 형식으로만 답하라. 다른 텍스트 금지.

{{"verdict": "즉시조치|백로그|학습|무관",
 "reason": "내 스택 중 어디에 해당하는지 1줄 (무관이면 빈 문자열)",
 "action": "이번 주 안에 할 구체적 작업 1개 — 명령어/설정변경/PoC 범위 수준 (무관이면 빈 문자열)",
 "tags": ["kebab-case-태그", "최대 3개"],
 "title_ko": "한국어 요약 제목"}}

판정 기준:
- 즉시조치: context의 스택에 직접 해당하는 보안 이슈나 breaking change
- 백로그: 스택에 해당되지만 긴급하지 않음
- 학습: 스택과 무관하나 context의 "관심 분야" 세부 주제에 해당
- 무관: 그 외 전부. context의 "명시적 제외" 항목은 반드시 무관.
  억지로 인사이트를 만들지 말 것

제목: {title}
출처: {source_name}
링크: {url}
본문/요약: {summary}"""


def log(msg: str) -> None:
    print(msg, flush=True)


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "item"
    return slug[:60].rstrip("-")


# ---------------------------------------------------------------- collection

def collect_rss(feeds: list) -> list:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    for feed in feeds:
        try:
            parsed = feedparser.parse(
                feed["url"], agent=USER_AGENT, request_headers={"Accept": "*/*"}
            )
            count = 0
            for e in parsed.entries:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                if ts:
                    published = datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc)
                    if published < cutoff:
                        continue
                link = e.get("link", "")
                if not link:
                    continue
                items.append({
                    "title": strip_html(e.get("title", "(no title)")),
                    "url": link,
                    "summary": strip_html(e.get("summary", ""))[:SUMMARY_MAX_CHARS],
                    "source_name": feed["name"],
                })
                count += 1
            log(f"  [rss] {feed['name']}: {count}건")
        except Exception as exc:  # noqa: BLE001 — 소스 하나가 전체를 죽이면 안 됨
            log(f"  [rss] {feed['name']}: 실패 ({exc})")
    return items


def collect_reddit(subs: list) -> list:
    items = []
    for sub in subs:
        url = (
            f"https://www.reddit.com/r/{sub['subreddit']}/{sub.get('listing', 'top')}.json"
            f"?t={sub.get('t', 'day')}&limit=25"
        )
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            count = 0
            for child in resp.json().get("data", {}).get("children", []):
                post = child.get("data", {})
                if post.get("score", 0) < sub.get("min_score", 0):
                    continue
                if post.get("stickied"):
                    continue
                permalink = "https://www.reddit.com" + post.get("permalink", "")
                summary = strip_html(post.get("selftext", ""))[:SUMMARY_MAX_CHARS]
                if not summary and post.get("url"):
                    summary = f"링크 포스트: {post['url']}"
                items.append({
                    "title": post.get("title", "(no title)"),
                    "url": permalink,
                    "summary": summary,
                    "source_name": f"r/{sub['subreddit']}",
                })
                count += 1
            log(f"  [reddit] r/{sub['subreddit']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [reddit] r/{sub['subreddit']}: 실패 ({exc})")
    return items


def collect_hackernews(queries: list) -> list:
    items = []
    for q in queries:
        url = f"https://hnrss.org/newest?q={requests.utils.quote(q['query'])}&points={q.get('min_points', 50)}"
        try:
            parsed = feedparser.parse(url, agent=USER_AGENT)
            count = 0
            for e in parsed.entries:
                link = e.get("link", "")
                if not link:
                    continue
                items.append({
                    "title": strip_html(e.get("title", "(no title)")),
                    "url": link,
                    "summary": strip_html(e.get("summary", ""))[:SUMMARY_MAX_CHARS],
                    "source_name": f"HN ({q['query']})",
                })
                count += 1
            log(f"  [hn] {q['query']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [hn] {q['query']}: 실패 ({exc})")
    return items


def collect_github(searches: list) -> list:
    items = []
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for search in searches:
        since = (datetime.now(timezone.utc) - timedelta(days=search.get("days_back", 7))).date()
        query = f"{search['query']} created:>{since.isoformat()}"
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 10},
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            count = 0
            for repo in resp.json().get("items", []):
                desc = repo.get("description") or ""
                items.append({
                    "title": f"{repo['full_name']} (★{repo.get('stargazers_count', 0)})",
                    "url": repo["html_url"],
                    "summary": desc[:SUMMARY_MAX_CHARS],
                    "source_name": "GitHub Trending",
                })
                count += 1
            log(f"  [github] {search['query']}: {count}건")
        except Exception as exc:  # noqa: BLE001
            log(f"  [github] {search['query']}: 실패 ({exc})")
    return items


def interleave_by_source(items: list) -> list:
    """소스별 라운드로빈 — MAX_ITEMS 컷에서 특정 소스가 독식하지 않도록."""
    buckets: dict[str, list] = {}
    for item in items:
        buckets.setdefault(item["source_name"], []).append(item)
    result = []
    while any(buckets.values()):
        for name in list(buckets):
            if buckets[name]:
                result.append(buckets[name].pop(0))
    return result


# ------------------------------------------------------------------ judgment

class FatalAPIError(Exception):
    """재시도가 무의미한 오류(크레딧 부족, 인증 실패) — 실행 전체 중단."""


def is_fatal_api_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in (
        "credit balance", "authenticat", "invalid x-api-key",
        "invalid api key", "invalid bearer token", "oauth token", "/login",
        "401",
    ))


def parse_judgment(text: str) -> dict | None:
    """모델 응답에서 JSON을 추출·검증. 실패 시 None."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("verdict") not in VERDICTS:
        return None
    if not isinstance(data.get("title_ko"), str) or not data["title_ko"].strip():
        return None
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    data["tags"] = [slugify(str(t)) for t in tags[:3] if str(t).strip()]
    data["reason"] = str(data.get("reason", "")).strip()
    data["action"] = str(data.get("action", "")).strip()
    return data


def build_prompt(item: dict) -> str:
    return JUDGE_PROMPT.format(
        title=item["title"],
        source_name=item["source_name"],
        url=item["url"],
        summary=item["summary"] or "(요약 없음 — 제목으로 판단)",
    )


def judge_item_api(client, model: str, system_blocks: list, item: dict) -> dict | None:
    prompt = build_prompt(item)
    for attempt in (1, 2):  # JSON 파싱 실패 시 1회 재시도
        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            if is_fatal_api_error(exc):
                raise FatalAPIError(str(exc)) from exc
            log(f"    API 오류 (시도 {attempt}): {exc}")
            if attempt == 2:
                return None
            time.sleep(3)
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        judgment = parse_judgment(text)
        if judgment:
            return judgment
        log(f"    JSON 파싱 실패 (시도 {attempt}): {text[:120]!r}")
    return None


def judge_item_cli(model: str, system_text: str, item: dict) -> dict | None:
    """Claude Code CLI(claude -p)로 판정 — API 크레딧 대신 구독 인증 사용."""
    prompt = build_prompt(item)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # CLI가 API 키 과금으로 빠지지 않도록
    cmd = ["claude", "-p", "--model", model, "--tools", "",
           "--output-format", "text", "--append-system-prompt", system_text]
    for attempt in (1, 2):
        try:
            result = subprocess.run(cmd, input=prompt, env=env, timeout=180,
                                    capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log(f"    CLI 타임아웃 (시도 {attempt})")
            continue
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            if is_fatal_api_error(RuntimeError(err)):
                raise FatalAPIError(err[:300])
            log(f"    CLI 오류 (시도 {attempt}): {err[:200]}")
            if attempt == 2:
                return None
            time.sleep(3)
            continue
        judgment = parse_judgment(result.stdout)
        if judgment:
            return judgment
        log(f"    JSON 파싱 실패 (시도 {attempt}): {result.stdout[:120]!r}")
    return None


# -------------------------------------------------------------------- output

def yaml_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_post(item: dict, judgment: dict, date: datetime) -> Path:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{date.date().isoformat()}-{slugify(item['title'])}"
    path = CONTENT_DIR / f"{base}.md"
    n = 2
    while path.exists():
        path = CONTENT_DIR / f"{base}-{n}.md"
        n += 1
    tags = ", ".join(yaml_quote(t) for t in judgment["tags"])
    body = f"""---
title: {yaml_quote(judgment["title_ko"])}
date: {date.isoformat()}
verdict: {yaml_quote(judgment["verdict"])}
tags: [{tags}]
source: {yaml_quote(item["url"])}
source_name: {yaml_quote(item["source_name"])}
status: "대기"
---
- **근거:** {judgment["reason"]}
- **액션:** {judgment["action"]}
"""
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------- main

def load_processed() -> dict:
    if PROCESSED_FILE.exists():
        try:
            return json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("processed.json 파싱 실패 — 초기화")
    return {}


def prune_processed(processed: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PROCESSED_TTL_DAYS)).isoformat()
    return {k: v for k, v in processed.items() if v >= cutoff}


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily insight pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="파일 생성/processed.json 갱신 없이 판정 결과만 출력")
    parser.add_argument("--max-items", type=int,
                        default=int(os.environ.get("MAX_ITEMS", "30")),
                        help="1회 실행당 판정 최대 건수")
    args = parser.parse_args()

    # 판정 백엔드: claude-code(구독 인증, 기본) 또는 api(ANTHROPIC_API_KEY 과금)
    backend = os.environ.get("JUDGE_BACKEND", "").strip() or (
        "claude-code" if shutil.which("claude") else "api"
    )
    client = None
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log("오류: api 백엔드에는 ANTHROPIC_API_KEY 환경변수가 필요합니다")
            return 1
        import anthropic  # 지연 임포트 — claude-code 백엔드에서는 불필요

        client = anthropic.Anthropic()
    elif backend == "claude-code":
        if not shutil.which("claude"):
            log("오류: claude-code 백엔드에는 claude CLI가 PATH에 있어야 합니다")
            return 1
    else:
        log(f"오류: 알 수 없는 JUDGE_BACKEND={backend!r} (claude-code | api)")
        return 1

    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    feeds = yaml.safe_load(FEEDS_FILE.read_text(encoding="utf-8"))
    context_md = CONTEXT_FILE.read_text(encoding="utf-8")

    system_text = (
        "당신은 아래 투자 컨텍스트를 기준으로 투자 뉴스의 실행 가치를 판정하는 투자 리서치 어시스턴트다.\n\n"
        + context_md
    )
    # api 백엔드: context.md는 모든 호출에서 동일 → prompt cache로 비용 절감
    system_blocks = [{
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }]

    log(f"=== 수집 시작 (backend={backend}, model={model}, max_items={args.max_items}, dry_run={args.dry_run}) ===")
    collected = []
    collected += collect_rss(feeds.get("rss", []))
    collected += collect_reddit(feeds.get("reddit", []))
    collected += collect_hackernews(feeds.get("hackernews", []))
    collected += collect_github(feeds.get("github_search", []))

    # URL 기준 중복 제거 (동일 실행 내)
    seen_urls = set()
    unique = []
    for item in collected:
        h = url_hash(item["url"])
        if h in seen_urls:
            continue
        seen_urls.add(h)
        item["hash"] = h
        unique.append(item)

    processed = prune_processed(load_processed())
    fresh = [i for i in unique if i["hash"] not in processed]
    queue = interleave_by_source(fresh)[: args.max_items]

    log(f"\n수집 {len(collected)}건 / 중복 제거 후 {len(unique)}건 / 신규 {len(fresh)}건 / 판정 대상 {len(queue)}건\n")

    now = datetime.now(timezone.utc).astimezone()  # 로컬(러너) 타임존 ISO
    verdict_counts = {v: 0 for v in VERDICTS}
    skipped = 0
    created_files = []

    fatal_error = None
    for i, item in enumerate(queue, 1):
        log(f"[{i}/{len(queue)}] {item['source_name']} | {item['title'][:80]}")
        try:
            if backend == "claude-code":
                judgment = judge_item_cli(model, system_text, item)
            else:
                judgment = judge_item_api(client, model, system_blocks, item)
        except FatalAPIError as exc:
            fatal_error = exc
            break
        if judgment is None:
            skipped += 1
            log("    → 스킵 (판정 실패)")
            continue
        verdict_counts[judgment["verdict"]] += 1
        log(f"    → {judgment['verdict']} | {judgment['title_ko']}")
        if judgment["reason"]:
            log(f"      근거: {judgment['reason']}")
        if judgment["action"]:
            log(f"      액션: {judgment['action']}")

        if not args.dry_run:
            processed[item["hash"]] = now.isoformat()
            if judgment["verdict"] != "무관":
                created_files.append(write_post(item, judgment, now))

    if not args.dry_run:
        PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROCESSED_FILE.write_text(
            json.dumps(processed, indent=1, sort_keys=True), encoding="utf-8"
        )

    log("\n=== 실행 결과 요약 ===")
    judged = sum(verdict_counts.values())
    log(f"수집: {len(collected)}건 / 신규: {len(fresh)}건 / 판정: {judged}건 (실패 스킵 {skipped}건)")
    log("판정 분포: " + " / ".join(f"{v} {c}" for v, c in verdict_counts.items()))
    if args.dry_run:
        log("(dry-run — 파일 생성/기록 갱신 없음)")
    elif created_files:
        log("생성 파일:")
        for f in created_files:
            log(f"  - {f.relative_to(ROOT)}")
    else:
        log("생성 파일 없음")

    if fatal_error:
        log(f"\n중단: 복구 불가능한 API 오류 — {fatal_error}")
        log("→ Anthropic 크레딧/API 키를 확인하세요. 미처리 항목은 다음 실행에서 재시도됩니다.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
