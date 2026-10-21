"""보안 게이트 — 요청이 어느 모델 경로를 탈 수 있는지를 코드로 정한다.

LLM이 아니라 정책이 결정한다. 판정이 애매하거나 정책이 닫혀 있으면 항상 "local"이다.
"""
from __future__ import annotations

import os

from .policy import Policy

LANGSMITH_CLOUD = "https://api.smith.langchain.com"


def is_sensitive(policy: Policy, repo: str, request_sensitive: bool = False) -> bool:
    return request_sensitive or repo in policy.sensitive_repos


def decide(policy: Policy, endpoint: str | None, sensitive: bool, attested: bool) -> str:
    """"local" 또는 "cloud"를 반환한다. cloud는 모든 조건을 통과할 때만 나온다."""
    if sensitive or not policy.cloud_enabled:
        return "local"
    if endpoint is None or endpoint not in policy.allowed_endpoints:
        return "local"
    if policy.require_attestation and not attested:
        return "local"
    return "cloud"


def tracing_endpoint() -> str:
    return (os.environ.get("LANGSMITH_ENDPOINT") or os.environ.get("LANGCHAIN_ENDPOINT") or LANGSMITH_CLOUD).rstrip("/")


def tracing_decision(policy: Policy, sensitive: bool, endpoint: str | None = None) -> tuple[bool, str]:
    """LangSmith 추적 허용 여부를 '추적 데이터가 어디에 저장되는가'로 정한다.

    - 정책이 끄면 항상 끔
    - 추적 서버가 사내 주소(자체 호스팅·BYOC, policy의 internal_endpoints)면 민감 저장소도 허용 — 데이터가 회사 밖으로 안 나간다
    - 클라우드 주소면 민감 저장소는 끄고, 비민감 저장소는 입출력 숨김일 때만 허용(메타데이터만 전송)
    판정이 애매하면 끈다(fail-closed): 주소는 정확히 일치할 때만 사내로 본다.
    """
    if not policy.langsmith_enabled:
        return False, "policy_off"
    ep = (endpoint or tracing_endpoint()).rstrip("/")
    internal = {e.rstrip("/") for e in policy.langsmith_internal_endpoints}
    if ep in internal:
        return True, "internal_endpoint"
    if sensitive:
        return False, "sensitive_on_cloud"
    if not policy.hide_inputs_outputs:
        return False, "cloud_requires_hidden_io"
    return True, "cloud_metadata_only"


def tracing_allowed(policy: Policy, sensitive: bool, endpoint: str | None = None) -> bool:
    return tracing_decision(policy, sensitive, endpoint)[0]
