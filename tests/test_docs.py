"""문서 근거 — 절 분할·검색, 문서 속 호출 관계의 결정적 대조(5가지 판정), 에이전트 문서 경로."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.analyzers.base import CallEdge, CallGraph, Symbol
from backend.app.analyzers.loader import analyze_repo
from backend.app.agents.graph import _unique_named_symbol_ids
from backend.app.retrieval.docs import (
    check_claim, check_section, extract_claims, load_sections, relate_symbol, search_sections,
)

ROOT = Path(__file__).resolve().parent.parent / "var" / "data" / "repos"


@pytest.fixture(scope="module")
def py():
    root = ROOT / "sample_py"
    return analyze_repo("sample_py", root), load_sections(root)


def _sec(secs, title):
    return next(s for s in secs if s.title.startswith(title))


def test_sections_split_by_heading(py):
    _, secs = py
    titles = [s.title for s in secs]
    assert "1단계: 주문 검증" in titles and "5단계: 감사 로그 기록" in titles
    assert all(s.file == "docs/order_spec.md" for s in secs)


def test_extract_claims_only_code_names():
    text = "`A.b`가 `C.d`를 호출한다.\n결제 모듈을 부른다.\n`X` → `Y.z`"
    assert extract_claims(text) == [("A.b", "C.d"), ("X", "Y.z")]     # 자연어 '결제 모듈'은 잡지 않는다


def test_document_evidence_skips_ambiguous_qualnames():
    functions = [
        {"id": "app/a.py::record", "qualname": "record"},
        {"id": "snapshot/a.py::record", "qualname": "record"},
        {"id": "app/a.py::save", "qualname": "save"},
    ]
    assert _unique_named_symbol_ids(functions, ["record", "save", "missing"]) == ["app/a.py::save"]


@pytest.mark.parametrize("title,expected", [
    ("2단계", {"match"}),
    ("3단계", {"match", "indirect"}),
    ("5단계", {"missing"}),           # 문서가 말하는 AuditLogger.write는 코드에 없다(낡은 문서)
    ("주문 취소", {"match", "mismatch"}),   # cancel은 send_receipt를 부르지 않는다
    ("확장 훅", {"unknown"}),           # run_hook은 getattr 동적 호출 → 없다고 단정하지 않는다
])
def test_check_verdicts(py, title, expected):
    g, secs = py
    got = {c["verdict"] for c in check_section(g, _sec(secs, title))["claims"]}
    assert got == expected


def test_ambiguous_interface_is_unknown_not_mismatch():
    root = ROOT / "sample_java"
    g, secs = analyze_repo("sample_java", root), load_sections(root)
    claims = {c["callee"]: c["verdict"] for c in check_section(g, secs[0])["claims"]}
    assert claims["CardPaymentGateway.pay"] == "unknown"     # 구현체 2개 → 모호
    assert claims["Notifier.send"] == "match"


def test_doc_prefers_langgraph_node_suffix_over_unrelated_method():
    """아키텍처 문서는 "explain → verify"처럼 LangGraph에 짧은 키로 등록된 노드 이름을 적지만,
    실제 함수는 `explain_node`다. 저장소 다른 곳의 무관한 동명 메서드(OllamaQualityLLM.explain,
    OpenAIQualityLLM.explain)와 겹쳐 '같은 이름이 여러 개'로 잘못 판정되면 안 된다."""
    def sym(id_, name, qualname):
        return Symbol(id=id_, name=name, qualname=qualname, language="python", file="m.py",
                      start_line=1, end_line=1, signature=f"def {name}()")

    g = CallGraph(repo="r", symbols={
        s.id: s for s in [
            sym("m.py::explain_node", "explain_node", "explain_node"),
            sym("m.py::verify_node", "verify_node", "verify_node"),
            sym("m.py::Ollama.explain", "explain", "OllamaQualityLLM.explain"),
            sym("m.py::OpenAI.explain", "explain", "OpenAIQualityLLM.explain"),
        ]
    }, edges=[
        CallEdge(caller="m.py::explain_node", callee="m.py::verify_node",
                  callee_text="conditional(...):explain->verify", line=1, resolved=True, kind="graph"),
    ])
    claim = check_claim(g, "explain", "verify")
    assert claim["verdict"] == "match"
    assert claim["caller"] == "explain_node" and claim["callee"] == "verify_node"


def test_search_prefers_step_number(py):
    _, secs = py
    hits = search_sections("3단계 결제 요청에 해당하는 코드가 PaymentClient.request 맞아?", secs)
    assert hits[0]["title"] == "3단계: 결제 요청"


def test_relate_symbol(py):
    g, secs = py
    chk = check_section(g, _sec(secs, "3단계"))
    assert relate_symbol(g, "PaymentClient.request", chk)["relation"] == "direct"
    assert relate_symbol(g, "PaymentClient._post", chk)["relation"] == "linked"   # request → _post
    assert relate_symbol(g, "StockService.increase", chk)["relation"] == "none"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    with TestClient(server.app) as c:
        yield c


def _chat(client, message, repo="sample_py"):
    r = client.post("/chat", json={"message": message, "repo": repo})
    assert r.status_code == 200, r.text
    return r.json()


def test_doc_route_answers_with_doc_and_check_zones(client):
    out = _chat(client, "기획서의 3단계 결제 요청에 해당하는 코드가 PaymentClient.request 맞아?")
    reply = out["reply"]
    assert out["metadata"]["route"] == "doc" and out["metadata"]["mode"] == "doc"
    assert "[문서 근거 — docs/order_spec.md · 3단계: 결제 요청" in reply
    assert "✓ 일치: OrderService.create → PaymentClient.request" in reply
    assert "✓ 일치(간접): OrderService.create → _api_key" in reply
    assert "PaymentClient.request: 문서 절에 이름이 직접 적혀 있음" in reply
    assert "[해석 — " in reply and out["verification"]["ok"]
    types = [e["type"] for e in out["events"]]
    assert types[:2] == ["GateDecided", "RouteDecided"] and "DocumentCandidates" in types and "DocCheck" in types


def test_doc_route_flags_stale_document(client):
    reply = _chat(client, "문서에 있는 5단계 감사 로그 기록이 코드랑 맞아?")["reply"]
    assert "✗ 코드에 없는 이름: OrderService.create → AuditLogger.write" in reply
    assert "담당자 확인" in reply



def test_hallucinated_doc_explanation_is_dropped(client, monkeypatch):
    import server
    llm = server.app.state.deps.quality_llm
    orig = llm.explain_doc

    async def bad(query, section, check):
        out, usage = await orig(query, section, check)
        out["claims"].append({"caller": "OrderService.create", "callee": "Ghost.run"})
        return out, usage
    monkeypatch.setattr(llm, "explain_doc", bad)
    out = _chat(client, "기획서의 2단계 재고 차감 맞아?")
    assert out["metadata"]["mode"] == "doc-no-llm" and "[해석 없음" in out["reply"]
    assert "✓ 일치: OrderService.create → StockService.decrease" in out["reply"]   # 결정적 대조는 남는다


def test_source_endpoint_and_answer_symbols(client):
    import server
    r = client.get("/source/sample_py", params={"symbol": "PaymentClient.request"})
    assert r.status_code == 200 and r.json()["file"] == "orders/payment.py" and "def request" in r.json()["source"]
    assert client.get("/source/not_allowed", params={"symbol": "x"}).status_code == 403
    assert client.get("/source/sample_py", params={"symbol": "Ghost.run"}).status_code == 404
    ev = [e for e in server._read_audit() if e["event"] == "source"][-2]
    assert ev["symbol"] == "PaymentClient.request" and "source" not in ev          # 코드 본문은 감사 로그에 없다
    out = _chat(client, "기획서의 3단계 결제 요청에 해당하는 코드가 PaymentClient.request 맞아?")
    labels = {ref["label"] for ref in out["symbols"]}
    assert {"PaymentClient.request", "OrderService.create", "_api_key"} <= labels
    assert "AuditLogger.write" not in {ref["label"] for ref in _chat(client, "문서에 있는 5단계 감사 로그 기록이 코드랑 맞아?")["symbols"]}  # 없는 이름은 버튼 안 만듦


def test_file_endpoint_whole_file_with_highlight(client):
    """함수 하나가 아니라 그 함수가 든 파일 전체 — 함수 줄 범위와 파일 안 함수 목록이 같이 온다."""
    import server
    r = client.get("/file/sample_py", params={"symbol": "PaymentClient.request"})
    assert r.status_code == 200
    d = r.json()
    assert d["file"] == "orders/payment.py" and d["start_line"] == 1
    a, b = d["highlight"]
    assert a <= b and "def request" in d["source"].splitlines()[a - 1]
    assert any(f["qualname"] == "PaymentClient.request" for f in d["functions"])
    assert d["line_count"] == len(d["source"].splitlines()) or d["line_count"] == len(d["source"].split("\n"))
    # path로 열기: 인덱스된 소스 파일만 허용
    assert client.get("/file/sample_py", params={"path": "orders/payment.py"}).status_code == 200
    for bad in ("../../.env", "policy.yaml", "docs/order_spec.md", "/etc/passwd"):
        assert client.get("/file/sample_py", params={"path": bad}).status_code == 404
    assert client.get("/file/sample_py").status_code == 400
    assert client.get("/file/not_allowed", params={"path": "x.py"}).status_code == 403
    ev = [e for e in server._read_audit() if e["event"] == "file"]
    assert ev and all("source" not in e for e in ev)
