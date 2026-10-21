"""실제 Ollama + 실제 MCP + HTTP API를 임시 데이터로 검증한다.
실행: .venv/bin/python -m eval.smoke_ollama [--stream]
실제 모델 응답을 사용하며 fake·클라우드로 대체하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true", help="추가로 실제 모델 SSE 응답도 확인")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["CLOUD_BACKEND"] = ""
    os.environ["SUPERVISOR_VERSION"] = "v2"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    endpoint = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    if urlparse(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("이 스모크 테스트는 로컬 Ollama 주소만 허용합니다.")
    from tools.eval.run_eval import _check_ollama
    _check_ollama()
    from fastapi.testclient import TestClient
    from backend.app.security import audit
    results = []
    from backend.app.repositories.testing import isolated_database
    with isolated_database():
        import server
        from backend.app.agents.ollama_llm import OllamaQualityLLM
        server._POLICY.langsmith_enabled = False
        key = os.environ.get("RECAT_API_KEY", "")
        headers = {"Authorization": "Bearer " + key} if key else {}
        with TestClient(server.app) as client:
            assert client.get("/health").json()["llm_backend"] == "ollama"
            assert isinstance(server.app.state.deps.quality_llm, OllamaQualityLLM)
            cases = [
                ("code", "OrderService.create 호출 흐름을 알려줘"),
                ("missing", "DefinitelyMissingClass.nonexistent 호출 흐름을 알려줘"),
                ("document", "문서 order_spec의 주문 생성 단계와 코드를 비교해줘"),
            ]
            for label, message in cases:
                started = time.monotonic()
                response = client.post("/chat", headers=headers, json={
                    "repo": "sample_py", "thread_id": "smoke-" + label, "message": message})
                assert response.status_code == 200, f"{label}: HTTP {response.status_code}"
                body = response.json()
                assert body.get("reply"), f"{label}: empty reply"
                mode = body["metadata"].get("mode")
                if label == "code":
                    assert body.get("structure"), "code: missing static structure"
                    assert "OrderService.create" in body["reply"]
                    assert mode != "no-llm-fallback", "code: model explanation fell back; investigate Ollama output"
                    assert any(e.get("type") == "Explained" for e in body["events"]), "code: no model explanation event"
                elif label == "missing":
                    assert not body.get("structure"), "missing function was replaced with another entry"
                else:
                    assert body["metadata"].get("route") == "doc", "document question was not routed to docs"
                result = {"case": label, "status": response.status_code, "mode": mode,
                          "seconds": round(time.monotonic() - started, 2)}
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.stream:
                started = time.monotonic()
                response = client.get("/chat/stream", headers=headers, params={
                    "repo": "sample_py", "thread_id": "smoke-stream",
                    "message": "OrderService.create 호출 흐름을 알려줘"})
                assert response.status_code == 200
                data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
                assert data and data[-1] == "[DONE]"
                events = [json.loads(item) for item in data if item != "[DONE]"]
                assert not any(e.get("type") == "Error" for e in events)
                assert any(e.get("type") == "FinalAnswer" and e.get("reply") for e in events)
                results.append({"case": "stream", "status": 200,
                                "seconds": round(time.monotonic() - started, 2)})
    print(json.dumps({"backend": "ollama", "passed": len(results), "cases": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
