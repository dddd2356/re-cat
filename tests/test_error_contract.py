"""모든 HTTP 오류의 공통 응답 계약과 요청 상관관계를 검증한다."""
from fastapi.testclient import TestClient

import server


def test_auth_error_has_stable_code_and_request_id(monkeypatch):
    monkeypatch.setenv("RECAT_API_KEY", "test-user-key")
    with TestClient(server.app) as client:
        response = client.get("/repos", headers={"X-Request-ID": "request-test-1"})
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "request-test-1"
    assert response.json() == {"error": {
        "code": "AUTH_INVALID", "message": "API 키가 없거나 올바르지 않습니다.",
        "request_id": "request-test-1",
    }}


def test_validation_error_never_echoes_request_body(monkeypatch):
    monkeypatch.setenv("RECAT_API_KEY", "test-user-key")
    with TestClient(server.app) as client:
        response = client.post("/chat", headers={"Authorization": "Bearer test-user-key"},
                               json={"message": "", "repo": "sample_py"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert "detail" not in body and "message" not in str(body.get("error", {}).get("detail", ""))


def test_unexpected_error_uses_common_contract_without_exposing_exception():
    import asyncio
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/test", "headers": [],
                       "query_string": b"", "server": ("test", 80), "scheme": "http",
                       "client": ("test", 1), "http_version": "1.1"})
    request.state.request_id = "request-unexpected-1"
    response = asyncio.run(server.unexpected_error_handler(request, RuntimeError("private detail")))
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    import json
    assert json.loads(response.body) == {"error": {
        "code": "INTERNAL_ERROR", "message": "요청을 처리할 수 없습니다.",
        "request_id": "request-unexpected-1",
    }}


def test_audit_route_accepts_admin_key_only(monkeypatch):
    import asyncio
    import httpx

    monkeypatch.setenv("RECAT_API_KEY", "user-key")
    monkeypatch.setenv("RECAT_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(server.audit, "read", lambda n: [])

    async def exercise():
        transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            no_key = await client.get("/audit")
            user = await client.get("/audit", headers={"Authorization": "Bearer user-key"})
            admin = await client.get("/audit", headers={"Authorization": "Bearer admin-key"})
        return no_key, user, admin

    no_key, user, admin = asyncio.run(exercise())
    assert no_key.status_code == user.status_code == 401
    assert admin.status_code == 200 and admin.json() == {"entries": []}
