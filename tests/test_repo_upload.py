"""zip 업로드 — 경로 이탈·링크·압축 폭탄 차단, 최상위 폴더 벗기기, 실패 시 흔적 없음, API 권한·교체 규칙."""
import io
import shutil
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.security.repo_upload import Limits, UploadRejected, extract_repo

PY = "def a():\n    return b()\n\ndef b():\n    return 1\n"


def _zip(tmp_path, entries, name="u.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, data in entries:
            if isinstance(data, zipfile.ZipInfo):
                z.writestr(data, "x")
            else:
                z.writestr(arc, data)
    return p


def test_extract_strips_single_top_folder_and_skips_junk(tmp_path):
    root = tmp_path / "repos"; root.mkdir()
    z = _zip(tmp_path, [("svc/app/m.py", PY), ("svc/README.md", "# doc"), ("svc/.git/config", "x"),
                        ("__MACOSX/svc/._m.py", "x"), ("svc/node_modules/x.js", "x")])
    out = extract_repo(z, "svc", root)
    assert out["files"] == 2
    assert (root / "svc" / "app" / "m.py").exists() and not (root / "svc" / ".git").exists()
    assert not (root / "svc" / "svc").exists()


@pytest.mark.parametrize("bad", ["../evil.py", "/etc/passwd", "a/../../evil.py", "C:/win.py"])
def test_zip_slip_rejected_and_nothing_left(tmp_path, bad):
    root = tmp_path / "repos"; root.mkdir()
    z = _zip(tmp_path, [("ok.py", PY), (bad, "x")])
    with pytest.raises(UploadRejected):
        extract_repo(z, "svc", root)
    assert list(root.iterdir()) == []                 # 임시 폴더도 남지 않는다
    assert not (tmp_path / "evil.py").exists()


def test_symlink_rejected(tmp_path):
    root = tmp_path / "repos"; root.mkdir()
    info = zipfile.ZipInfo("link.py")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(UploadRejected):
        extract_repo(_zip(tmp_path, [("ok.py", PY), ("link.py", info)]), "svc", root)


def test_symlink_in_skipped_dependency_directory_is_ignored(tmp_path):
    root = tmp_path / "repos"; root.mkdir()
    info = zipfile.ZipInfo("node_modules/pkg/link.js")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    extract_repo(_zip(tmp_path, [("app.py", PY), ("node_modules/pkg/link.js", info)]), "svc", root)
    assert (root / "svc" / "app.py").exists()
    assert not (root / "svc" / "node_modules").exists()


def test_bomb_and_count_limits(tmp_path):
    root = tmp_path / "repos"; root.mkdir()
    big = _zip(tmp_path, [("big.txt", "0" * (3 * 1024 * 1024))], "big.zip")     # 3MB가 수 KB로 압축됨
    with pytest.raises(UploadRejected, match="압축률"):
        extract_repo(big, "svc", root)
    many = _zip(tmp_path, [(f"f{i}.py", "x=1\n") for i in range(6)], "many.zip")
    with pytest.raises(UploadRejected, match="너무 많"):
        extract_repo(many, "svc", root, lim=Limits(max_files=5))
    with pytest.raises(UploadRejected, match="zip 파일이 아닙니다"):
        (tmp_path / "x.zip").write_bytes(b"not a zip")
        extract_repo(tmp_path / "x.zip", "svc", root)
    with pytest.raises(UploadRejected, match="이름"):
        extract_repo(_zip(tmp_path, [("a.py", PY)], "n.zip"), "../x", root)


def test_upload_limits_are_configurable_but_capped(monkeypatch):
    monkeypatch.setenv("RECAT_UPLOAD_MAX_ZIP_MB", "400")
    monkeypatch.setenv("RECAT_UPLOAD_MAX_TOTAL_MB", "99999")
    monkeypatch.setenv("RECAT_UPLOAD_MAX_FILE_MB", "bad-value")
    lim = Limits()
    assert lim.max_zip_bytes == 400 * 1024 * 1024
    assert lim.max_total_bytes == 2048 * 1024 * 1024
    assert lim.max_file_bytes == 100 * 1024 * 1024


@pytest.mark.parametrize(("detail", "message"), [
    ("파일이 너무 큽니다: secret.jar", "파일별 크기 한도를 넘는"),
    ("허용되지 않는 경로: ../secret.py", "상대 경로 이탈"),
    ("링크·특수 파일은 올릴 수 없습니다: link", "심볼릭 링크"),
])
def test_upload_rejection_hides_archive_filenames(detail, message):
    from backend.app.main import _upload_rejection_message
    out = _upload_rejection_message(Exception(detail))
    assert message in out and "secret" not in out and "link" not in out


# ---- API ----
NAME = "_tmp_upload_repo"
ADMIN, USER = {"Authorization": "Bearer admin-key"}, {"Authorization": "Bearer user-key"}


@pytest.fixture
def client(monkeypatch):
    import server
    from backend.app.security.path_guard import REPOS_ROOT
    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    try:
        with TestClient(server.app) as c:
            yield c
    finally:
        shutil.rmtree(REPOS_ROOT / NAME, ignore_errors=True)
        for lst in (server._POLICY.allowed_repos, server._POLICY.sensitive_repos):
            while NAME in lst:
                lst.remove(NAME)


def _zbytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _up(c, data, headers=ADMIN, **form):
    return c.post("/admin/repos/upload", headers=headers, files={"file": ("r.zip", data, "application/zip")},
                  data={"repo": NAME, **{k: str(v).lower() for k, v in form.items()}})


def test_upload_registers_sensitive_by_default_and_is_usable(client):
    assert _up(client, _zbytes({"proj/m.py": PY}), headers=USER).status_code == 401
    r = _up(client, _zbytes({"proj/m.py": PY, "proj/docs/spec.md": "## 1단계\n`a` → `b`\n"}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "upload" and body["sensitive"] is True and body["function_count"] == 2
    assert body["doc_sections"] == 1
    assert client.get(f"/graph/{NAME}", headers=USER).status_code == 200


def test_upload_sensitive_only_off_when_explicit(client):
    r = _up(client, _zbytes({"m.py": PY}), sensitive="garbage")   # 애매한 값은 민감으로 둔다
    assert r.status_code == 200 and r.json()["sensitive"] is True


def test_upload_duplicate_and_replace_rules(client):
    assert _up(client, _zbytes({"m.py": PY})).status_code == 200
    assert _up(client, _zbytes({"m.py": PY})).status_code == 400                       # 같은 이름 재업로드는 거부
    r = _up(client, _zbytes({"m.py": PY + "\ndef c():\n    return 2\n"}), replace=True)  # 업로드 저장소는 교체 가능
    assert r.status_code == 200 and r.json()["function_count"] == 3
    bad = client.post("/admin/repos/upload", headers=ADMIN, files={"file": ("r.zip", _zbytes({"m.py": PY}), "application/zip")},
                      data={"repo": "sample_py", "replace": "true"})
    assert bad.status_code == 400                                                      # policy 저장소는 교체 불가


def test_rejected_upload_is_audited_without_content(client):
    import server
    r = _up(client, _zbytes({"../evil.py": "x"}))
    assert r.status_code == 400
    ev = [e for e in server._read_audit() if e["event"] == "repo_upload_rejected"][-1]
    assert ev["repo"] == NAME and "source" not in ev
