import os
import sys
import uuid
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server
from tests.mock_llm import factory as _mock_llm_factory
server._build_llms = _mock_llm_factory

@pytest.fixture(autouse=True)
def _inject_llm_mock(monkeypatch):
    """HTTP/LLM 경계는 테스트 double로만 대체한다. 제품은 Ollama 전용이다."""
    monkeypatch.setattr(server, "_build_llms", _mock_llm_factory)
    # 개발자 .env의 키가 테스트 인증 상태를 바꾸지 않도록 격리한다.
    monkeypatch.delenv("RECAT_API_KEY", raising=False)
    monkeypatch.delenv("RECAT_ADMIN_KEY", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _postgres_test_schema():
    """실제 PostgreSQL 임시 스키마. MCP도 환경변수로 같은 격리 공간을 본다."""
    from backend.app.repositories import db
    from backend.app.repositories.migrate import migrate
    from psycopg import sql
    name = "recat_test_" + uuid.uuid4().hex
    old = os.environ.get("RECAT_DB_SCHEMA")
    os.environ["RECAT_DB_SCHEMA"] = name
    migrate()
    try:
        yield
    finally:
        with db.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        if old is None:
            os.environ.pop("RECAT_DB_SCHEMA", None)
        else:
            os.environ["RECAT_DB_SCHEMA"] = old


@pytest.fixture(autouse=True)
def _isolated_database(_postgres_test_schema, monkeypatch):
    from backend.app.repositories import db
    # 자동 회귀는 임베딩 모델을 요구하지 않는다. 실제 하이브리드는 별도 통합 평가.
    monkeypatch.setenv("RECAT_DOC_SEARCH", "keyword")
    with db.connection() as conn:
        conn.execute("TRUNCATE audit_event,message,document_embedding,deletion_job RESTART IDENTITY")
        conn.execute("DELETE FROM repository WHERE source <> 'policy'")
