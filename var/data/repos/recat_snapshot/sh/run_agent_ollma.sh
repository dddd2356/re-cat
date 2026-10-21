#!/usr/bin/env bash
# Recat — 로컬 Ollama 모델로 기동. 코드는 이 PC 밖으로 나가지 않는다 (policy.yaml cloud.enabled=false).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
[ -f .env ] || cp .env.example .env
# .env는 server.py가 python-dotenv로 읽는다. 셸에서 source하지 않는다(<, &, 공백 같은 문자가 든 값이 깨지지 않게).
env_get() { sed -n "s/^$1=//p" .env | tail -1 | sed 's/^["'\'']//; s/["'\'']$//'; }
FAST_MODEL="${OLLAMA_FAST_MODEL:-$(env_get OLLAMA_FAST_MODEL)}"; FAST_MODEL="${FAST_MODEL:-qwen3:0.6b}"
QUALITY_MODEL="${OLLAMA_QUALITY_MODEL:-$(env_get OLLAMA_QUALITY_MODEL)}"; QUALITY_MODEL="${QUALITY_MODEL:-qwen3:8b}"
curl -s --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
  || { echo "ERROR: ollama serve가 떠 있지 않습니다 — 'bash sh/setup_ollama.sh' 먼저"; exit 1; }
for m in "$FAST_MODEL" "$QUALITY_MODEL"; do
  ollama list | grep "^${m}" >/dev/null || { echo "ERROR: 모델 $m 미설치 — 'ollama pull $m'"; exit 1; }
done
lsof -ti:8100 | xargs kill -9 2>/dev/null || true
echo "Recat 기동: http://127.0.0.1:8100 (LLM_BACKEND=ollama, fast=$FAST_MODEL, quality=$QUALITY_MODEL)"
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 exec python3 server.py
