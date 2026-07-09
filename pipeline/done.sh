#!/usr/bin/env bash
# 주간 리뷰용: front matter의 status: "대기" → "완료"
# usage: pipeline/done.sh content/insights/2026-07-09-some-post.md
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <content/insights/파일.md> [...]"; exit 1; }
for f in "$@"; do
  sed -i.bak 's/^status: "대기"/status: "완료"/' "$f" && rm -f "$f.bak"
  echo "완료 처리: $f"
done
