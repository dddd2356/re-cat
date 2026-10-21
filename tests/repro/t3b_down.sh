TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
echo "③-b 모델 서버가 계속 503 (재시도로도 안 됨)"
echo '{"fail": 1000}' > $TS/mode.json; bash $TS/chat.sh "OrderService.cancel 호출 흐름"
python3 -c "import json;d=json.load(open('/tmp/r.json'));print('  이벤트:', [e['type']+('('+e.get('stage','')+')' if e['type']=='LLMUnavailable' else '') for e in d['events']])"
echo "  잡담:"; bash $TS/chat.sh "안녕" | tail -1
echo '{}' > $TS/mode.json
