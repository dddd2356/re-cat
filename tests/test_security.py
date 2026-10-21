import pytest

from backend.app.security.gate import decide, is_sensitive, tracing_allowed, tracing_decision
from backend.app.security.masking import mask
from backend.app.security.path_guard import PathEscape, repo_root, safe_file
from backend.app.security.policy import Policy


def _policy(**kw):
    base = dict(cloud_enabled=True, allowed_endpoints=["https://api.openai.com/v1"],
                require_attestation=True, sensitive_repos=["secret_repo"],
                allowed_repos=["ok_repo", "secret_repo"], langsmith_enabled=True)
    base.update(kw)
    return Policy(**base)


EP = "https://api.openai.com/v1"


def test_default_policy_is_local():
    assert decide(Policy(), EP, False, True) == "local"


def test_cloud_only_when_every_condition_passes():
    p = _policy()
    assert decide(p, EP, False, True) == "cloud"
    assert decide(p, EP, True, True) == "local"                      # 민감
    assert decide(p, EP, False, False) == "local"                    # 확인서 없음
    assert decide(p, "https://evil.example/v1", False, True) == "local"
    assert decide(p, None, False, True) == "local"
    assert decide(_policy(cloud_enabled=False), EP, False, True) == "local"


def test_sensitive_repo_disables_tracing():
    p = _policy()
    assert is_sensitive(p, "secret_repo")
    assert not tracing_allowed(p, True)
    assert tracing_allowed(p, False)
    assert not tracing_allowed(_policy(langsmith_enabled=False), False)


INTERNAL = "https://langsmith.corp.local/api"


def test_tracing_depends_on_where_traces_are_stored():
    p = _policy(langsmith_internal_endpoints=[INTERNAL + "/"])
    # 사내 추적 서버 → 민감 저장소도 허용 (데이터가 회사 밖으로 안 나감)
    assert tracing_decision(p, True, INTERNAL) == (True, "internal_endpoint")
    # 클라우드 → 민감은 끔, 비민감은 입출력 숨김일 때만
    assert tracing_decision(p, True, "https://api.smith.langchain.com") == (False, "sensitive_on_cloud")
    assert tracing_decision(p, False, "https://api.smith.langchain.com") == (True, "cloud_metadata_only")
    assert tracing_decision(_policy(hide_inputs_outputs=False), False, "https://api.smith.langchain.com") == \
        (False, "cloud_requires_hidden_io")
    # 비슷한 주소는 사내로 보지 않는다 (정확히 일치할 때만)
    assert tracing_decision(p, True, "https://langsmith.corp.local.evil.com/api")[0] is False
    # 정책 스위치가 꺼져 있으면 사내 주소여도 끔
    assert tracing_decision(_policy(langsmith_enabled=False, langsmith_internal_endpoints=[INTERNAL]), True, INTERNAL) == \
        (False, "policy_off")


def test_tracing_endpoint_from_env(monkeypatch):
    p = _policy(langsmith_internal_endpoints=[INTERNAL])
    monkeypatch.setenv("LANGSMITH_ENDPOINT", INTERNAL)
    assert tracing_allowed(p, True)
    monkeypatch.delenv("LANGSMITH_ENDPOINT")
    assert not tracing_allowed(p, True)          # 기본값은 클라우드


def test_masking_hides_secrets():
    src = ('API_KEY = "sk-abcdefghijklmnop1234"\npassword = "hunter2xyz"\n'
           'mail = "dev@corp.com"\nphone = "010-1234-5678"\n'
           'jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"\nk = "AKIAABCDEFGHIJKLMNOP"')
    out, n = mask(src)
    for secret in ("sk-abcdefghijklmnop1234", "hunter2xyz", "dev@corp.com", "010-1234-5678",
                   "eyJhbGciOiJIUzI1NiIs", "AKIAABCDEFGHIJKLMNOP"):
        assert secret not in out
    assert n >= 5


def test_masking_known_gap_base64_is_not_caught():
    import base64
    enc = base64.b64encode(b"sk-abcdefghijklmnop1234").decode()
    out, _ = mask(f'x = "{enc}"')
    assert enc in out          # 알려진 한계: 인코딩된 비밀은 못 잡는다 (카나리 실험에서 건수로 보고)


def test_path_guard():
    with pytest.raises(PathEscape):
        repo_root("../etc", ["../etc"])
    with pytest.raises(PermissionError):
        repo_root("sample_py", [])
    root = repo_root("sample_py", ["sample_py"])
    assert safe_file(root, "orders/service.py").exists()
    with pytest.raises(PathEscape):
        safe_file(root, "../../../server.py")


def test_admin_key_is_fail_closed(monkeypatch):
    from backend.app.core.errors import AppError
    from backend.app.security.auth import require_admin
    monkeypatch.delenv("RECAT_ADMIN_KEY", raising=False)
    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    with pytest.raises(AppError) as e:                            # 사용자 키만 있으면 관리자 기능은 닫힘
        require_admin("Bearer user-key")
    assert e.value.status_code == 403
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    with pytest.raises(AppError) as e:                            # 사용자 키로는 관리자 불가
        require_admin("Bearer user-key")
    assert e.value.status_code == 401
    assert require_admin("Bearer admin-key") is None
    monkeypatch.delenv("RECAT_ADMIN_KEY")
    monkeypatch.delenv("RECAT_API_KEY")
    assert require_admin(None) is None                            # 개발 모드
