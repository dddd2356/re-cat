"""폴더 업로드(브라우저 폴더 선택)와 저장소별 문서 등록."""
import shutil

import pytest
from fastapi.testclient import TestClient

NAME = "_tmp_folder_repo"
ADMIN, USER = {"Authorization": "Bearer admin-key"}, {"Authorization": "Bearer user-key"}
PY = "class Pay:\n    def run(self):\n        return self.send()\n\n    def send(self):\n        return 1\n"


@pytest.fixture
def client(monkeypatch):
    import server
    from backend.app.security.path_guard import DOCS_ROOT, REPOS_ROOT
    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    try:
        with TestClient(server.app) as c:
            yield c
    finally:
        shutil.rmtree(REPOS_ROOT / NAME, ignore_errors=True)
        shutil.rmtree(DOCS_ROOT / NAME, ignore_errors=True)
        shutil.rmtree(DOCS_ROOT / "sample_py", ignore_errors=True)
        for lst in (server._POLICY.allowed_repos, server._POLICY.sensitive_repos):
            while NAME in lst:
                lst.remove(NAME)


def _folder(c, files, headers=ADMIN):
    return c.post("/admin/repos/upload-folder", headers=headers, data={"repo": NAME},
                  files=[("files", (path, body, "text/plain")) for path, body in files])


def test_folder_upload_keeps_relative_paths(client):
    assert _folder(client, [("proj/pay.py", PY)], headers=USER).status_code == 401
    r = _folder(client, [("proj/pay.py", PY), ("proj/docs/spec.md", "## 결제\n`Pay.run` → `Pay.send`\n"),
                         ("proj/.git/HEAD", "x")])
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["source"] == "upload" and b["sensitive"] is True and b["function_count"] == 2 and b["doc_sections"] == 1


def test_folder_upload_rejects_escape(client):
    r = _folder(client, [("proj/ok.py", PY), ("../evil.py", "x")])
    assert r.status_code == 400 and r.json()["error"]["code"] == "UPLOAD_REJECTED"


def test_docs_upload_list_delete_and_used_in_answers(client):
    r = client.post("/admin/repos/sample_py/docs", headers=ADMIN,
                    files=[("files", ("refund_policy.md", "## 환불 정책\n`OrderService.cancel` → `PaymentClient.refund`\n", "text/markdown"))])
    assert r.status_code == 200, r.text
    before = r.json()["doc_sections"]
    assert before >= 9 and r.json()["uploaded_docs"] == ["refund_policy.md"]
    assert client.get("/admin/repos/sample_py/docs", headers=ADMIN).json()["uploaded"][0]["name"] == "refund_policy.md"
    out = client.post("/chat", headers=USER, json={"message": "문서의 환불 정책이 코드랑 맞아?", "repo": "sample_py"}).json()
    assert "uploaded/refund_policy.md" in out["reply"] and "✓ 일치: OrderService.cancel → PaymentClient.refund" in out["reply"]
    assert client.delete("/admin/repos/sample_py/docs/refund_policy.md", headers=ADMIN).json()["doc_sections"] == before - 1
    assert client.delete("/admin/repos/sample_py/docs/..%2F..%2Fserver.py", headers=ADMIN).status_code == 404


@pytest.mark.parametrize("fname,body", [("evil.exe", "x"), (".hidden.md", "x"), ("bin.md", b"\xff\xfe\x00")])
def test_docs_upload_rejects_bad_files(client, fname, body):
    r = client.post("/admin/repos/sample_py/docs", headers=ADMIN, files=[("files", (fname, body, "text/plain"))])
    assert r.status_code == 400


def test_docs_upload_needs_admin_and_allowed_repo(client):
    f = [("files", ("a.md", "# a", "text/markdown"))]
    assert client.post("/admin/repos/sample_py/docs", headers=USER, files=f).status_code == 401
    assert client.post("/admin/repos/not_allowed/docs", headers=ADMIN, files=f).status_code == 403
