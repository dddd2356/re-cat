"""관리자 저장소 삭제: 실제 MCP subprocess와 격리된 세션으로 검증."""
import io
import shutil
import zipfile

import pytest
from fastapi.testclient import TestClient

import server
import mcp_server as M
from backend.app.agents.session_store import PostgresSessionStore
from backend.app.security import registry
from backend.app.security.path_guard import REPOS_ROOT, DOCS_ROOT

NAME = "_tmp_delete_repo"
ADMIN = {"Authorization": "Bearer delete-admin"}
USER = {"Authorization": "Bearer delete-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECAT_API_KEY", "delete-user")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "delete-admin")
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    with TestClient(server.app) as c:
        yield c
    for root in (REPOS_ROOT / NAME, DOCS_ROOT / NAME):
        shutil.rmtree(root, ignore_errors=True)
    registry.forget_policy(server._POLICY, NAME)


def upload(client):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("a.py", "def f():\n    return 1\n")
        z.writestr("README.md", "# Guide\n`f` returns one.\n")
    r = client.post("/admin/repos/upload", headers=ADMIN,
                    data={"repo": NAME}, files={"file": ("repo.zip", data.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text


def delete(client, name=NAME, headers=ADMIN, confirm=None):
    return client.request("DELETE", f"/admin/repos/{name}", headers=headers,
                          json={"confirm_name": name if confirm is None else confirm})


def test_delete_removes_every_store_and_allows_same_name_upload(client):
    upload(client)
    r = client.post(f"/admin/repos/{NAME}/docs", headers=ADMIN,
                    files={"files": ("spec.md", b"# Spec\n`f` returns one.", "text/markdown")})
    assert r.status_code == 200, r.text
    # 같은 thread_id여도 다른 저장소 이력은 보존.
    store = server.app.state.deps.session_store
    store.append("same", "user", "private", NAME)
    store.append("same", "assistant", "answer", NAME)
    store.append("same", "user", "keep", "sample_py")
    store.append("legacy-associated", "user", "old private")
    from backend.app.security import audit
    audit.record("chat", repo=NAME, thread_id="legacy-associated")
    assert client.get(f"/graph/{NAME}", headers=USER).status_code == 200
    r = delete(client)
    assert r.status_code == 200, r.text
    assert r.json()["files"] == 3
    assert not (REPOS_ROOT / NAME).exists()
    assert not (DOCS_ROOT / NAME).exists()
    assert NAME not in registry.registered()
    assert NAME not in server._POLICY.allowed_repos
    assert NAME not in server._POLICY.sensitive_repos
    assert store.load("same", NAME) == []
    assert store.load("legacy-associated") == []
    assert store.load("same", "sample_py") == [{"role": "user", "content": "keep"}]
    assert client.get(f"/graph/{NAME}", headers=USER).status_code == 403
    # 서버와 별개인 MCP 프로세스도 허용 목록을 잊었는지 직접 확인.
    with pytest.raises(Exception, match="not allowed"):
        client.portal.call(server.app.state.deps.mcp.call_tool, "get_status", {"repo": NAME})
    event = [e for e in server._read_audit() if e["event"] == "repo_delete"][-1]
    assert set(event) == {"ts", "event", "repo", "files"}
    assert event["repo"] == NAME and event["files"] == 3
    upload(client)
    assert NAME in registry.registered()
    assert client.get(f"/graph/{NAME}", headers=USER).status_code == 200


def test_user_cannot_delete_and_name_must_match(client):
    upload(client)
    assert delete(client, headers=USER).status_code in (401, 403)
    assert delete(client, confirm="wrong").status_code == 400
    assert NAME in registry.registered() and (REPOS_ROOT / NAME).exists()


def test_policy_repo_cannot_be_deleted(client):
    assert delete(client, name="sample_py").status_code == 400
    assert (REPOS_ROOT / "sample_py").is_dir()
    assert "sample_py" in server._POLICY.allowed_repos


def test_mcp_failure_restores_files_and_registration(client, monkeypatch):
    upload(client)
    original = server.app.state.deps.mcp.call_tool
    async def failing(name, arguments, **kwargs):
        if name == "forget_repo":
            raise RuntimeError("injected MCP failure")
        return await original(name, arguments, **kwargs)
    monkeypatch.setattr(server.app.state.deps.mcp, "call_tool", failing)
    with pytest.raises(RuntimeError, match="injected"):
        delete(client)
    assert NAME in registry.registered()
    assert NAME in server._POLICY.allowed_repos
    assert (REPOS_ROOT / NAME / "a.py").is_file()


def test_mcp_forget_clears_graph_docs_and_timestamp(monkeypatch):
    from backend.app.analyzers.base import CallGraph
    M._cache[NAME] = CallGraph(repo=NAME)
    M._docs[NAME] = ["indexed"]
    M._analyzed_at[NAME] = 1.0
    M.POLICY.allowed_repos.append(NAME)
    M.POLICY.sensitive_repos.append(NAME)
    result = M.forget_repo(NAME)
    assert result == {"repo": NAME, "cached": False, "allowed": False}
    assert all(NAME not in d for d in (M._cache, M._docs, M._analyzed_at))
    assert NAME not in M.POLICY.sensitive_repos



def test_admin_proxy_forwards_delete_body_and_browser_key(monkeypatch):
    import web_demo_server as frontend
    from fastapi.responses import JSONResponse
    captured = {}
    async def forward(method, path, authorization, body=b"", content_type="application/json"):
        captured.update(method=method, path=path, authorization=authorization, body=body)
        return JSONResponse({"repo": NAME, "files": 3})
    monkeypatch.setattr(frontend, "_admin_forward", forward)
    with TestClient(frontend.app) as c:
        r = c.request("DELETE", f"/admin/repos/{NAME}", headers=ADMIN,
                      json={"confirm_name": NAME})
    import json
    assert r.status_code == 200
    assert captured["method"] == "DELETE"
    assert captured["path"] == f"/admin/repos/{NAME}"
    assert captured["authorization"] == ADMIN["Authorization"]
    assert json.loads(captured["body"]) == {"confirm_name": NAME}
