# 모의 Ollama(11999) + Re-cat(8100, LLM_BACKEND=ollama) 기동
TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
[ -x "$R/.venv/bin/python" ] && export PATH="$R/.venv/bin:$PATH"
(lsof -ti:8100; lsof -ti:11999) | xargs -r kill -9 2>/dev/null; sleep 0.5
echo '{}' > $TS/mode.json
(nohup python $TS/mock_ollama.py > $TS/mock.out 2>&1 &)
cd "$R" && rm -f session.db
(RECAT_MCP_TIMEOUT=${RECAT_MCP_TIMEOUT:-60} OLLAMA_URL=http://127.0.0.1:11999/api/chat LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 RECAT_API_KEY= nohup python server.py > $TS/server.out 2>&1 &)
for i in $(seq 1 30); do curl -s localhost:8100/health >/dev/null && break; sleep 0.5; done
curl -s localhost:8100/health >/dev/null || { echo "서버 기동 실패 — $TS/server.out 확인"; tail -5 "$TS/server.out"; exit 1; }
echo "준비됨: 모의 Ollama :11999, Re-cat :8100"
