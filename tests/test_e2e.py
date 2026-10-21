"""전체 흐름 테스트 — fake 백엔드, 실제 MCP subprocess, 실제 정적 분석."""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""   # .env의 키가 load_dotenv로 덮어쓰지 않게 빈 값으로 고정
    import server
    with TestClient(server.app) as c:
        yield c


def _chat(client, message, **kw):
    r = client.post("/chat", json={"message": message, "repo": "sample_py", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_trace_question_verifies_mock_explanation(client):
    out = _chat(client, "OrderService.create 호출 흐름 설명해줘")
    types = [e["type"] for e in out["events"]]
    assert types.count("Explained") == 1
    verifs = [e for e in out["events"] if e["type"] == "Verification"]
    assert verifs[0]["ok"] is True
    assert "AuditLogger" not in out["reply"].split("[해석")[1]
    assert "[구조 — 정적 분석으로 확인됨]" in out["reply"]
    assert out["metadata"]["decision"] == "local"


def test_korean_question_finds_entry_point_by_search(client):
    out = _chat(client, "주문 생성은 어떻게 동작해?")
    ep = next(e for e in out["events"] if e["type"] == "EntryPoint")
    assert ep["symbol"] == "OrderService.create"


def test_greeting_skips_pipeline(client):
    out = _chat(client, "안녕")
    assert out["metadata"]["mode"] == "chat"
    assert not any(e["type"] == "ExploreDone" for e in out["events"])


def test_unknown_question_reports_not_found(client):
    out = _chat(client, "zzzz qqqq 함수 설명")
    assert out["metadata"]["mode"] == "not_found"



def test_sensitive_repo_is_forced_local_and_untraced(client):
    r = client.post("/chat", json={"message": "OrderService.create 설명", "repo": "sample_java",
                                   "attested": True})
    meta = r.json()["metadata"]
    assert meta["sensitive"] is True and meta["decision"] == "local"
    gate = next(e for e in r.json()["events"] if e["type"] == "GateDecided")
    assert gate["tracing"] is False


def test_ambiguous_java_call_is_reported_not_guessed(client):
    r = client.post("/chat", json={"message": "OrderService.create 호출 흐름", "repo": "sample_java"})
    reply = r.json()["reply"]
    assert "paymentGateway.pay (ambiguous)" in reply
    assert "CardPaymentGateway" not in reply


def test_repo_allowlist_403_and_flows_endpoint(client):
    r = client.post("/chat", json={"message": "x", "repo": "not_allowed"})
    assert r.status_code == 403
    f = client.get("/flows/sample_py", params={"symbol": "OrderService.create"})
    assert f.status_code == 200 and f.json()["pairs"]


def test_auth_required_when_key_configured(client, monkeypatch):
    monkeypatch.setenv("RECAT_API_KEY", "k-123")
    assert client.get("/repos").status_code == 401
    assert client.get("/repos", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/repos", headers={"Authorization": "Bearer k-123"}).status_code == 200


def test_audit_log_requires_admin_and_has_metadata_only(client, monkeypatch):
    _chat(client, "OrderService.cancel 흐름")
    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    assert client.get("/audit").status_code == 401
    assert client.get("/audit", headers={"Authorization": "Bearer user-key"}).status_code == 401
    response = client.get("/audit", headers={"Authorization": "Bearer admin-key"})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert entries and all("source" not in e and "snippet" not in e for e in entries)
    assert any(e["event"] == "chat" and "message_chars" in e for e in entries)


def test_stream_emits_tokens_events_and_done(client):
    with client.stream("GET", "/chat/stream",
                       params={"message": "OrderService.create 호출 흐름", "repo": "sample_py"}) as r:
        body = "".join(r.iter_text())
    events = [json.loads(l[6:]) for l in body.split("\n\n") if l.startswith("data: ") and "[DONE]" not in l]
    kinds = {e["type"] for e in events}
    assert {"GateDecided", "EntryPoint", "AnswerToken", "Verification", "FinalAnswer"} <= kinds
    assert body.rstrip().endswith("[DONE]")


def test_tool_budget_caps_exploration(client):
    deps = client.app.state.deps
    old = deps.policy.max_tool_calls
    deps.policy.max_tool_calls = 4
    try:
        out = _chat(client, "OrderService.create 호출 흐름")
    finally:
        deps.policy.max_tool_calls = old
    assert out["metadata"]["tool_calls"] <= 4
    assert any(e["type"] == "ToolBudgetReached" for e in out["events"])
    # 상한에 걸려 일부만 탐색했어도 주장은 전체 체인 기준으로 검증된다
    assert out["verification"]["ok"] is True


def test_fixed_exploration_mode_skips_llm_selection(client):
    deps = client.app.state.deps
    deps.explore_mode = "fixed"
    try:
        out = _chat(client, "OrderService.create 호출 흐름")
    finally:
        deps.explore_mode = "agent"
    done = next(e for e in out["events"] if e["type"] == "ExploreDone")
    assert done["mode"] == "fixed"
