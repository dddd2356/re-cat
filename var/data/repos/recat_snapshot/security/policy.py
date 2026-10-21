"""policy.yaml 로더 — 파일이 없거나 깨져 있으면 가장 닫힌 기본값을 쓴다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy.yaml"


@dataclass
class Policy:
    cloud_enabled: bool = False
    allowed_endpoints: list[str] = field(default_factory=list)
    require_attestation: bool = True
    require_masking: bool = True
    max_snippet_chars: int = 4000
    langsmith_enabled: bool = False
    hide_inputs_outputs: bool = True
    langsmith_internal_endpoints: list[str] = field(default_factory=list)   # 사내(자체 호스팅·BYOC) 추적 서버
    allowed_repos: list[str] = field(default_factory=list)
    sensitive_repos: list[str] = field(default_factory=list)
    max_tool_calls: int = 12
    max_depth: int = 3
    max_explain_retries: int = 2


def load_policy(path: Path = POLICY_PATH) -> Policy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return Policy()
    cloud, tracing = raw.get("cloud") or {}, raw.get("tracing") or {}
    repos, agent = raw.get("repos") or {}, raw.get("agent") or {}
    return Policy(
        cloud_enabled=bool(cloud.get("enabled", False)),
        allowed_endpoints=list(cloud.get("allowed_endpoints") or []),
        require_attestation=bool(cloud.get("require_attestation", True)),
        require_masking=bool(cloud.get("require_masking", True)),
        max_snippet_chars=int(cloud.get("max_snippet_chars", 4000)),
        langsmith_enabled=bool(tracing.get("langsmith_enabled", False)),
        hide_inputs_outputs=bool(tracing.get("hide_inputs_outputs", True)),
        langsmith_internal_endpoints=list(tracing.get("internal_endpoints") or []),
        allowed_repos=list(repos.get("allowed") or []),
        sensitive_repos=list(repos.get("sensitive") or []),
        max_tool_calls=int(agent.get("max_tool_calls", 12)),
        max_depth=int(agent.get("max_depth", 3)),
        max_explain_retries=int(agent.get("max_explain_retries", 2)),
    )
