"""API 키 인증과 저장소 인가 — FastAPI 의존성으로 쓴다.

RECAT_API_KEY가 설정돼 있으면 모든 요청에 Bearer 키가 필요하다(없으면 401).
허용 목록에 없는 저장소는 403이다. 키가 설정되지 않은 개발 모드에서는 인증을 건너뛰며,
이는 서버가 127.0.0.1에만 열려 있을 때만 의미가 있다.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


def require_key(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("RECAT_API_KEY")
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def require_repo(repo: str, allowed: list[str]) -> None:
    if repo not in allowed:
        raise HTTPException(status_code=403, detail=f"repo not allowed: {repo}")


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """관리자 엔드포인트용. 일반 사용자 키(RECAT_API_KEY)와 별도의 RECAT_ADMIN_KEY가 필요하다.

    fail-closed: 사용자 키는 설정돼 있는데 관리자 키가 없으면 관리자 기능을 열지 않는다(403).
    두 키가 모두 없는 개발 모드에서만 인증 없이 허용한다.
    """
    admin = os.environ.get("RECAT_ADMIN_KEY")
    if not admin:
        if os.environ.get("RECAT_API_KEY"):
            raise HTTPException(status_code=403, detail="admin key not configured")
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, admin):
        raise HTTPException(status_code=401, detail="missing or invalid admin key")
