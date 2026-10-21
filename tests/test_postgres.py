"""실제 PostgreSQL에서 보존·트랜잭션·삭제·검색 격리를 검증한다."""
import pytest
from backend.app.agents.session_store import PostgresSessionStore
from backend.app.repositories import db
from backend.app.security import audit


def test_restart_persists_and_turn_is_atomic():
    a=PostgresSessionStore()
    a.append_turn("thread","question","answer","sample_py")
    a.close()
    b=PostgresSessionStore()
    assert b.load("thread","sample_py")==[{"role":"user","content":"question"},{"role":"assistant","content":"answer"}]
    assert b.load("thread","sample_java")==[]
    with pytest.raises(Exception):
        with db.transaction():
            b.append("broken","user","must rollback","sample_py")
            b.append("broken","invalid-role","bad","sample_py")
    assert b.load("broken","sample_py")==[]


def test_audit_does_not_store_payloads_or_exception_text():
    audit.record("chat_error",repo="sample_py",detail="SECRET_CODE",source="SECRET_SOURCE",query="SECRET_QUERY",password="SECRET_KEY",error="RuntimeError")
    row=audit.read()[-1]
    assert row["repo"]=="sample_py" and row["error"]=="RuntimeError"
    assert not any("SECRET" in str(v) for v in row.values())


def test_missing_database_has_no_file_fallback(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://invalid:invalid@127.0.0.1:1/no_db")
    with pytest.raises(RuntimeError,match="PostgreSQL connection failed"):
        db.check()


def test_embedding_namespaced_and_model_errors_are_visible(monkeypatch):
    from backend.app.retrieval.docs import Section
    from backend.app.retrieval import hybrid
    vectors=[[1.0]+[0.0]*1023]
    monkeypatch.setattr(hybrid,"_embed",lambda texts:vectors*len(texts))
    monkeypatch.setenv("RECAT_DOC_SEARCH","hybrid")
    a=[Section("same#1","a.md","alpha",1,"alpha policy")]
    b=[Section("same#1","b.md","beta",1,"beta policy")]
    hits,meta=hybrid.search("sample_py","alpha",a)
    assert meta["vector_used"] and hits[0]["file"]=="a.md"
    hybrid.search("sample_java","beta",b)
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM document_embedding WHERE section_id='same#1'").fetchone()[0]==2
    import httpx
    def failed(texts):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(hybrid,"_embed",failed)
    hits,meta=hybrid.search("sample_py","alpha",a)
    assert not meta["vector_used"] and meta["method"]=="keyword-fallback" and hits


def test_embedding_hash_refresh_removes_deleted_sections(monkeypatch):
    from backend.app.retrieval.docs import Section
    from backend.app.retrieval import hybrid
    monkeypatch.setattr(hybrid,"_embed",lambda texts:[[1.0]+[0.0]*1023 for _ in texts])
    sections=[Section("old","a.md","old",1,"old"),Section("keep","a.md","keep",2,"keep")]
    hybrid.ensure_index("sample_py",sections)
    sections=[Section("keep","a.md","keep",2,"changed")]
    hybrid.ensure_index("sample_py",sections)
    with db.connection() as conn:
        rows=conn.execute("SELECT section_id FROM document_embedding WHERE repo='sample_py'").fetchall()
    assert rows==[("keep",)]
