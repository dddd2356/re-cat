# 사용: chat.sh "질문" [thread]  → 상태코드·소요시간·모드·검증 요약
rm -f /tmp/r.json; q="$1"; t="${2:-t$RANDOM}"
curl -s -o /tmp/r.json -w "%{http_code} %{time_total}s" -X POST -H 'Content-Type: application/json' localhost:8100/chat \
  -d "{\"message\":\"$q\",\"repo\":\"sample_py\",\"thread_id\":\"$t\"}" --max-time ${MAXT:-60}; echo
python3 - <<'P'
import json
try: d=json.load(open('/tmp/r.json'))
except Exception as e:
    import os; print('  (본문 없음/JSON 아님)', open('/tmp/r.json').read()[:200] if os.path.exists('/tmp/r.json') else '— 응답 없음'); raise SystemExit
if 'reply' not in d: print(' ', str(d)[:300]); raise SystemExit
v=d.get('verification') or {}
print('  mode=%s attempts=%s ok=%s verified=%s halluc=%s' % (d['metadata']['mode'], d['metadata']['explain_attempts'], v.get('ok'), v.get('verified'), v.get('hallucinated')))
print('  reply:', d['reply'][-160:].replace('\n',' | '))
P
