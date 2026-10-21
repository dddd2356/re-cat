"""진입점 탐색 보완 — 질의 확장, 되묻기(clarify), 도메인 단어 제거, 후보 제한에 대한 회귀 테스트.
실제 LLM 없이 fake 백엔드로 전부 재현한다."""
import os

import pytest
from fastapi.testclient import TestClient

from tests.mock_llm import MockFast as FastLLM
from backend.app.agents.graph import CLOSE_GAP, WEAK_TOP_SCORE, _should_clarify
from backend.app.retrieval.entrypoint_search import search


# ---- _should_clarify: 점수 의미에 맞춘 임계값 단위 테스트 ----

def test_tie_triggers_clarify():
    hits = [{"qualname": "A.x", "score": 1.0}, {"qualname": "B.y", "score": 1.0}]
    assert _should_clarify(hits)


def test_weak_single_hit_triggers_clarify():
    hits = [{"qualname": "A.x", "score": WEAK_TOP_SCORE - 0.5}]
    assert _should_clarify(hits)


def test_clear_winner_does_not_trigger_clarify():
    hits = [{"qualname": "A.x", "score": 10.0}, {"qualname": "B.y", "score": 0.5}]
    assert not _should_clarify(hits)


def test_one_word_gap_is_enough_to_decide():
    # 1등이 2등보다 겹치는 단어 하나(0.5점)만 더 있어도 가를 근거로 본다 — CLOSE_GAP 경계값
    hits = [{"qualname": "A.x", "score": WEAK_TOP_SCORE + CLOSE_GAP},
           {"qualname": "B.y", "score": WEAK_TOP_SCORE}]
    assert not _should_clarify(hits)


# ---- 도메인 단어 제거: 주문·결제·재고만으로는 더 이상 trace가 아니다 ----

def test_domain_words_alone_no_longer_route_to_trace():
    rule = FastLLM("rules", 0.0)
    for word in ("주문", "결제", "재고"):
        out, _ = rule.route(word)
        assert out["route"] == "chat", f"{word!r}이 여전히 trace로 분류됨(도메인 단어가 남아있음)"


def test_question_shaped_korean_still_routes_to_trace():
    rule = FastLLM("rules", 0.0)
    out, _ = rule.route("이건 어떻게 동작해?")   # 도메인 단어 없이도 질문 형식(어떻게/동작)만으로 trace
    assert out["route"] == "trace"


# ---- 후보 제한: search()가 돌려주는 후보는 항상 입력 functions 목록 안에 있다 ----

def test_candidates_are_always_a_subset_of_input_functions():
    functions = [{"id": "a", "qualname": "Foo.bar", "signature": "bar()", "doc": "결제를 요청한다", "file": "a.py"},
                {"id": "b", "qualname": "Baz.qux", "signature": "qux()", "doc": "재고를 차감한다", "file": "b.py"}]
    known_ids = {f["id"] for f in functions}
    for q in ["결제 요청은 어떻게 돼?", "재고 차감", "존재하지 않는 질문 xyz", "card payment gateway"]:
        hits = search(q, functions, extra_terms=["payment", "card"])
        assert all(h["id"] in known_ids for h in hits)   # 지어낸 이름은 나올 수 없다


# ---- 질의 확장: 검색 토큰에 그대로 더해지고, 이름 일치 판정에는 영향을 주지 않는다 ----

def test_extra_terms_add_to_overlap_score_not_name_match():
    functions = [{"id": "a", "qualname": "PaymentGateway.pay", "signature": "pay()", "doc": "", "file": "pay.py"}]
    without = search("이건 뭘 해?", functions)
    with_terms = search("이건 뭘 해?", functions, extra_terms=["payment", "pay"])
    assert not without                                   # 겹치는 게 전혀 없으면 후보 자체가 없다
    assert with_terms and with_terms[0]["score"] > 0      # 확장된 영어 키워드가 파일명·이름과 겹쳐 후보가 됨
    # 확장어만으로는 '정규 이름을 그대로 언급'(10점) 판정을 받지 않는다 — 원문 query만 그 검사에 쓴다
    assert with_terms[0]["score"] < 10


def test_file_path_is_a_search_target():
    functions = [{"id": "a", "qualname": "Helper.run", "signature": "run()", "doc": "", "file": "billing/payment.py"}]
    hits = search("payment", functions)
    assert hits and hits[0]["id"] == "a"   # 독스트링·이름에 없어도 파일 경로로 찾는다


# ---- locate_node 연동: 모델이 실패해도 기존 키워드 검색만으로 진행한다 ----

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    with TestClient(server.app) as c:
        yield c


def test_expand_query_failure_falls_back_to_keyword_search(client, monkeypatch):
    deps = client.app.state.deps

    def boom(query):
        raise RuntimeError("모델 서버 다운")
    monkeypatch.setattr(deps.fast_llm, "expand_query", boom)
    r = client.post("/chat", json={"message": "주문 생성은 어떻게 동작해?", "repo": "sample_py"})
    assert r.status_code == 200
    d = r.json()
    assert d["metadata"]["mode"] in ("agent", "no-llm-fallback")   # 확장 실패에도 진입점은 찾아 진행한다
    assert "OrderService.create" in d["reply"]


def test_expand_query_enriches_search_when_available(client, monkeypatch):
    deps = client.app.state.deps
    calls = []

    def fake_expand(query):
        calls.append(query)
        return ["payment", "gateway"], {"usd": 0.0}
    monkeypatch.setattr(deps.fast_llm, "expand_query", fake_expand)
    r = client.post("/chat", json={"message": "결제 요청은 어떻게 처리돼?", "repo": "sample_py"})
    assert r.status_code == 200
    assert calls == ["결제 요청은 어떻게 처리돼?"]        # locate_node가 원문 질문으로 확장을 요청했다
    assert "PaymentClient.request" in r.json()["reply"]  # 확장된 영어 키워드가 올바른 함수를 찾는 데 쓰였다


# ---- 되묻기(clarify): 동점이면 탐색하지 않고 후보를 보여준다 ----

def test_clarify_mode_offers_real_candidates_and_skips_explore(client, monkeypatch):
    import backend.app.agents.graph as graph_mod

    def tied_search(query, functions, top_k=3, extra_terms=None):
        return [{"id": "x1", "qualname": "A.foo", "score": 1.0, "doc": "", "signature": "", "file": "a.py"},
               {"id": "x2", "qualname": "B.bar", "score": 1.0, "doc": "", "signature": "", "file": "b.py"}]
    monkeypatch.setattr(graph_mod, "search", tied_search)
    r = client.post("/chat", json={"message": "이 기능은 어떻게 동작해?", "repo": "sample_py", "thread_id": "clarify1"})
    assert r.status_code == 200
    d = r.json()
    assert d["metadata"]["mode"] == "clarify"
    assert "어느 함수" in d["reply"]
    labels = {s["label"] for s in d["suggestions"]}
    assert labels == {"A.foo", "B.bar"}                           # 지어낸 후보가 아니라 search()가 돌려준 것 그대로
    assert all(s["ask"] == f"{s['label']} 호출 흐름" for s in d["suggestions"])
    assert not any(e["type"] in ("ExploreStep", "Explained") for e in d["events"])  # 탐색·설명을 시도하지 않았다
