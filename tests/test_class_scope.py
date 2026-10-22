"""클래스 전체 질문('OrderService 전체 흐름')과 이어지는 질문('init 말고 전체')."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.retrieval.entrypoint_search import class_scope

FUNCS = [{"qualname": q} for q in ("OrderService.__init__", "OrderService.create", "OrderService.cancel",
                                   "StockService.decrease", "send_receipt")]


@pytest.mark.parametrize("q,expect", [
    ("OrderService 전체 흐름이 뭐야", "OrderService"),
    ("OrderService가 하는 일 전부", "OrderService"),
    ("OrderService.create 호출 흐름", None),        # 메서드를 짚으면 함수 하나에서 시작
    ("OrderService의 cancel 흐름", None),
    ("send_receipt 흐름", None),
    ("주문 생성은 어떻게 동작해?", None),
])
def test_class_scope_detection(q, expect):
    got = class_scope(q, FUNCS)
    assert (got[0] if got else None) == expect
    if got:
        assert [m["qualname"] for m in got[1]] == ["OrderService.__init__", "OrderService.create", "OrderService.cancel"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    with TestClient(server.app) as c:
        yield c


def _chat(client, message, thread="t"):
    r = client.post("/chat", json={"message": message, "repo": "sample_py", "thread_id": thread})
    assert r.status_code == 200, r.text
    return r.json()


def test_class_question_covers_all_methods_and_verifies(client):
    out = _chat(client, "OrderService 전체 흐름이 뭐야", "a")
    s = out["structure"]
    assert s["scope"] == "class" and s["entry"] == "OrderService"
    assert {"OrderService.create", "OrderService.cancel", "OrderService.run_hook"} <= set(s["entries"])
    pairs = {tuple(p) for p in s["pairs"]}
    assert ("OrderService.create", "PaymentClient.request") in pairs and ("OrderService.cancel", "PaymentClient.refund") in pairs
    assert out["verification"]["ok"] and out["verification"]["verified"] >= len(pairs)   # 합집합 체인으로 검증
    assert "범위: OrderService 클래스 전체" in out["reply"]
    labels = {ref["label"] for ref in out["symbols"]}
    assert "OrderService" not in labels and "OrderService.cancel" in labels   # 코드 버튼은 실제 함수만


def test_followup_without_code_name_continues_previous_question(client):
    _chat(client, "OrderService 전체 흐름이 뭐야", "b")
    out = _chat(client, "init이 아니라 전체를 보여달라고", "b")
    assert any(e["type"] == "FollowUp" for e in out["events"])
    assert out["structure"]["scope"] == "class"
    assert _chat(client, "안녕", "c")["metadata"]["mode"] == "chat"          # 이전 코드 질문이 없으면 잡담 그대로
    _chat(client, "안녕", "d")
    assert _chat(client, "전체 보여줘", "d")["metadata"]["mode"] == "chat"   # 직전 사용자 질문이 코드 질문이 아니면 이어 붙이지 않음
