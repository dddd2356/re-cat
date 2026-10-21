# ① 모델이 5초 걸릴 때, 질문 처리 중에 /health 응답 시간
TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
echo '{"delay": 5}' > $TS/mode.json
(bash $TS/chat.sh "OrderService.cancel 호출 흐름" > /tmp/t1chat.txt 2>&1 &)
sleep 1
for i in 1 2 3; do curl -s -o /dev/null -w "  /health %{time_total}s\n" localhost:8100/health; done
sleep 25; echo "  질문:"; cat /tmp/t1chat.txt | head -2
echo '{}' > $TS/mode.json
