"""Ollama/OpenAI 어댑터는 모의 HTTP 서버로 요청 형식과 응답 파싱만 확인한다.
(실제 모델·실제 API로는 실행해 보지 않았다.)"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SEEN = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body, self.headers.get("Authorization")))
        schema = (body.get("format") or
                  (body.get("response_format") or {}).get("json_schema", {}).get("schema") or {})
        props = schema.get("properties", {})
        if "claims" in props:
            content = json.dumps({"explanation": "A가 B를 호출",
                                  "claims": [{"caller": "A", "callee": "B"}, {"bad": 1}]})
        elif "route" in props:
            content = json.dumps({"route": "trace"})
        elif "expand" in props:
            content = json.dumps({"expand": ["B", "없는이름"]})
        else:
            content = "안녕하세요"
        if self.path.endswith("/api/chat"):
            out = {"message": {"content": content}, "prompt_eval_count": 10, "eval_count": 5}
        else:
            out = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 15}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def mock_url():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


STRUCT = {"qualname": "A", "pairs": [["A", "B"]], "unresolved": [], "procedure": "절차"}


def test_ollama_adapter(mock_url, monkeypatch):
    from backend.app.agents import ollama_llm
    monkeypatch.setattr(ollama_llm, "OLLAMA_URL", mock_url + "/api/chat")
    fast, quality = ollama_llm.OllamaFastLLM("m-fast"), ollama_llm.OllamaQualityLLM("m-q")
    assert fast.route("A 흐름")[0]["route"] == "trace"
    chosen, usage = fast.select_expansions("q", ["B", "C"])
    assert chosen == ["B"] and usage["tokens"] == 15        # 후보에 없는 이름은 버린다
    out, _ = asyncio.run(quality.explain("q", STRUCT, {"A": "code"}, 1, None))
    assert out["claims"] == [{"caller": "A", "callee": "B"}]   # 형식이 틀린 주장은 걸러진다
    assert SEEN[-1][1]["format"]["properties"]["claims"]        # 구조화 출력 스키마를 보냈다


def test_openai_adapter(mock_url, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", mock_url)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from backend.app.agents.openai_llm import OpenAIQualityLLM
    q = OpenAIQualityLLM("gpt-x", 0.005)
    assert q.endpoint() == mock_url
    out, usage = asyncio.run(q.explain("q", STRUCT, {"A": "code"}, 1, "피드백"))
    assert out["explanation"] == "A가 B를 호출"
    assert usage["tokens"] == 15 and usage["usd"] > 0
    path, body, auth = SEEN[-1]
    assert path.endswith("/chat/completions") and auth == "Bearer sk-test"
    assert "피드백" in body["messages"][1]["content"]          # 검증 피드백이 프롬프트에 실렸다
