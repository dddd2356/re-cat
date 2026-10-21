#!/usr/bin/env bash
# Recat — 로컬 모델 + 클라우드(OpenAI 호환) 에스컬레이션 허용으로 기동.
# 외부 전송과 과금이 발생할 수 있다. policy.yaml의 cloud.enabled=true 이고, 요청에 attested=true가 있고,
# 저장소가 민감 목록에 없을 때만 클라우드 경로가 열린다 (보안 게이트).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
[ -f .env ] || cp .env.example .env
# .env는 server.py가 python-dotenv로 읽는다. 셸에서 source하지 않는다(<, &, 공백 같은 문자가 든 값이 깨지지 않게).
env_get() { sed -n "s/^$1=//p" .env | tail -1 | sed 's/^["'\'']//; s/["'\'']$//'; }
[ -n "${OPENAI_API_KEY:-$(env_get OPENAI_API_KEY)}" ] || { echo "ERROR: .env에 OPENAI_API_KEY를 넣어주세요"; exit 1; }
grep -q "enabled: true" policy.yaml || echo "참고: policy.yaml cloud.enabled=false 이므로 클라우드 경로는 열리지 않습니다."
read -r -p "코드 조각이 외부 API로 전송될 수 있고 과금됩니다. 계속할까요? (y/N) " ans
[ "$ans" = "y" ] || { echo "취소"; exit 1; }
lsof -ti:8100 | xargs kill -9 2>/dev/null || true
LLM_BACKEND="${LLM_BACKEND:-$(env_get LLM_BACKEND)}"; LLM_BACKEND="${LLM_BACKEND:-ollama}"; LLM_BACKEND="$LLM_BACKEND" CLOUD_BACKEND=openai SUPERVISOR_VERSION=v2 exec python3 -m backend.app.main
