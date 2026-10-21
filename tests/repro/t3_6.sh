TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
echo "③ 모델이 503(로딩 중)을 2번 돌려줄 때 — /chat"
echo '{"fail": 2}' > $TS/mode.json; bash $TS/chat.sh "OrderService.cancel 호출 흐름"
echo "④ 같은 상황 — 스트리밍(/chat/stream)"
echo '{"fail": 2}' > $TS/mode.json
curl -s -N --max-time 20 "localhost:8100/chat/stream?message=OrderService.cancel%20%ED%98%B8%EC%B6%9C&repo=sample_py&thread_id=s$RANDOM" | sed 's/^/  /' | cut -c1-140 | tail -4
echo "  감사 로그 마지막 이벤트: $(tail -1 $R/audit.log.jsonl | python3 -c 'import sys,json;print(json.load(sys.stdin)["event"])')"
echo "⑤ 모델이 깨진 JSON을 줄 때"
echo '{"bad_json": true}' > $TS/mode.json; bash $TS/chat.sh "OrderService.cancel 호출 흐름"
echo "⑥ 설명 본문에만 없는 함수(AuditLogger.write)를 쓰고 claims엔 안 넣을 때"
echo '{"fake_name": true}' > $TS/mode.json; bash $TS/chat.sh "OrderService.cancel 호출 흐름"
echo '{}' > $TS/mode.json
