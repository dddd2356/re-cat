#!/usr/bin/env bash
# Recat — 결정론 fake 백엔드로 기동 (모델·네트워크 불필요). 파이프라인 동작 확인용.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
[ -f .env ] || cp .env.example .env
# .env는 server.py가 python-dotenv로 읽는다. 셸에서 source하지 않는다(<, &, 공백 같은 문자가 든 값이 깨지지 않게).
env_get() { sed -n "s/^$1=//p" .env | tail -1 | sed 's/^["'\'']//; s/["'\'']$//'; }
lsof -ti:8100 | xargs kill -9 2>/dev/null || true
echo "Recat 기동: http://127.0.0.1:8100 (LLM_BACKEND=fake)"
LLM_BACKEND=fake SUPERVISOR_VERSION=v2 exec python3 server.py
