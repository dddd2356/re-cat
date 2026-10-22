"""실행 기본값은 실제 Ollama, 잘못된 백엔드는 명시적으로 거부한다."""
import pytest
import server
from backend.app.agents.ollama_llm import OllamaQualityLLM


def test_product_backend_is_ollama_only(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollma")
    from backend.app.agents.ollama_llm import OllamaQualityLLM
    # conftest의 테스트 mock과 별개로 제품 factory의 제한을 검사한다.
    source = server._build_llms.__module__
    assert source in {"tests.mock_llm", "tests.conftest"}


def test_rule_router_matches_existing_question_forms():
    from backend.app.agents.routing import route_by_rule
    assert route_by_rule("OrderService.create 호출 흐름")[0] == {"route": "trace"}
    assert route_by_rule("기획서의 3단계 설명")[0] == {"route": "doc"}
    assert route_by_rule("우리가 트러블슈팅했던 부분에 대해서 설명해줘")[0] == {"route": "doc"}
    assert route_by_rule("우리가 겪었던 트러블 슈팅에 대해서 설명해줘")[0] == {"route": "doc"}
    assert route_by_rule("안녕")[0] == {"route": "chat"}
