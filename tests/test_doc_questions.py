"""문서 내용을 묻는 질문 — '문서'라는 말이 없어도 문서로 답하고, 없는 함수 이름은 비슷한 함수로 대신 답하지 않는다."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    from backend.app.security import path_guard
    docs = path_guard.DOCS_ROOT / "sample_py"
    docs.mkdir(parents=True, exist_ok=True)
    # 공개된 고정 분석 fixture를 외부 문서로 업로드한다. gitignore된 private-docs에
    # 테스트가 의존하면 새 clone/CI에서 재현되지 않는다.
    source = ROOT / "var" / "data" / "repos" / "recat_snapshot" / "TROUBLESHOOTING.md"
    (docs / "TROUBLESHOOTING.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with TestClient(server.app) as c:
        c.post("/admin/repos/sample_py/reanalyze", headers={"Authorization": f"Bearer {os.environ.get('RECAT_ADMIN_KEY', 'dev-admin-key-change-me')}"})
        yield c
    (docs / "TROUBLESHOOTING.md").unlink()


def _chat(client, q):
    r = client.post("/chat", json={"message": q, "repo": "sample_py", "thread_id": "q" + str(abs(hash(q)))})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("q,section", [
    ("트러블슈팅에서 MCP 서브프로세스가 죽으면 어떻게 돼?", "② MCP"),      # 코드 질문으로 분류 → 함수 없음 → 문서
    ("동시 업로드 문제 원인이 뭐였어?", "⑦ 관리자 업로드"),                 # 잡담으로 분류 → 문서와 겹침 → 문서
    ("남은 한계가 뭐야?", "남은 한계"),
])
def test_doc_content_questions_reach_the_right_section(client, q, section):
    d = _chat(client, q)
    assert d["metadata"]["mode"] == "doc"
    sec = next(e for e in d["events"] if e["type"] == "DocSection")
    assert sec["section"].startswith(section) and sec["file"] == "uploaded/TROUBLESHOOTING.md"
    assert "에 따르면:" in d["reply"]                                        # 설명이 문서 내용으로 먼저 답한다


def test_greeting_stays_chat(client):
    assert _chat(client, "안녕")["metadata"]["mode"] == "chat"


@pytest.mark.parametrize("q", ["PaymentGateway.charge 흐름", "PaymentClient.charge 흐름"])
def test_unknown_named_function_is_not_replaced_by_a_lookalike(client, q):
    d = _chat(client, q)
    assert d["metadata"]["mode"] == "not_found" and "이 저장소에 없는 이름" in d["reply"]
    assert "structure" not in d


def test_known_function_still_traces(client):
    assert _chat(client, "OrderService.create 호출 흐름")["metadata"]["mode"] == "agent"
