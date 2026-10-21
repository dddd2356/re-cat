"""관리자 저장소 관리 — 권한 분리, 등록 경로 제한, 정책 완화 불가, 재분석 반영."""
import shutil

import pytest
from fastapi.testclient import TestClient

import server
from backend.app.security import registry
from backend.app.security.path_guard import REPOS_ROOT
from backend.app.security.policy import Policy

NAME = "_tmp_admin_test_repo"
ADMIN, USER = {"Authorization": "Bearer admin-key"}, {"Authorization": "Bearer user-key"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    d = REPOS_ROOT / NAME
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.py").write_text("def f():\n    return g()\n\ndef g():\n    return 1\n", encoding="utf-8")
    try:
        with TestClient(server.app) as c:
            yield c
    finally:
        shutil.rmtree(d, ignore_errors=True)
        for lst in (server._POLICY.allowed_repos, server._POLICY.sensitive_repos):
            while NAME in lst:
                lst.remove(NAME)


def test_user_key_cannot_use_admin_repos(client):
    assert client.get("/admin/repos", headers=USER).status_code == 401
    assert client.post("/admin/repos", json={"repo": NAME}, headers=USER).status_code == 401
    assert client.post("/admin/repos/sample_py/reanalyze", headers=USER).status_code == 401
    assert client.get("/admin/repos").status_code == 401


def test_list_shows_status_and_candidates(client):
    r = client.get("/admin/repos", headers=ADMIN).json()
    row = {x["repo"]: x for x in r["repos"]}["sample_py"]
    assert row["function_count"] > 0 and row["source"] == "policy" and row["analyzed_at"]
    assert NAME in r["candidates"]


def test_register_defaults_to_sensitive_and_becomes_usable(client):
    res = client.post("/admin/repos", json={"repo": NAME}, headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["sensitive"] is True and body["source"] == "registered" and body["function_count"] == 2
    assert client.get(f"/graph/{NAME}", headers=USER).status_code == 200      # MCP 서브프로세스도 허용
    assert NAME in registry.registered()


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", "sample_py", "sample_java", "no_such_folder"])
def test_register_rejects_escape_duplicates_and_missing(client, bad):
    assert client.post("/admin/repos", json={"repo": bad}, headers=ADMIN).status_code == 400
    assert not registry.registered()


def test_registry_cannot_loosen_policy():
    p = Policy(allowed_repos=["x"], sensitive_repos=["x"])
    with pytest.raises(registry.RegistryError):                  # 이미 허용된 저장소는 다시 등록해 민감 해제 불가
        registry.register(p, "x", sensitive=False)
    assert p.sensitive_repos == ["x"]


def test_apply_is_union_and_idempotent(client):
    client.post("/admin/repos", json={"repo": NAME, "sensitive": False}, headers=ADMIN)
    p = Policy(allowed_repos=["a"], sensitive_repos=["a"])
    registry.apply(p); registry.apply(p)
    assert p.allowed_repos == ["a", NAME] and p.sensitive_repos == ["a"]


def test_reanalyze_picks_up_changes_and_is_audited(client):
    client.post("/admin/repos", json={"repo": NAME}, headers=ADMIN)
    (REPOS_ROOT / NAME / "b.py").write_text("def h():\n    return 2\n", encoding="utf-8")
    res = client.post(f"/admin/repos/{NAME}/reanalyze", headers=ADMIN).json()
    assert res["before"]["function_count"] == 2 and res["after"]["function_count"] == 3
    ev = [e for e in server._read_audit() if e["event"] == "repo_reanalyze"]
    assert ev and ev[-1]["functions_after"] == 3 and "source" not in ev[-1]


def test_reanalyze_unknown_repo_is_forbidden(client):
    assert client.post("/admin/repos/not_allowed/reanalyze", headers=ADMIN).status_code == 403
