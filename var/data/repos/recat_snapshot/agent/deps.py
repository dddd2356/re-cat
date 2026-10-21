"""의존성 주입 — 노드가 쓰는 모든 의존성을 SupervisorDeps 하나에 명시적으로 담는다 (Base 구조 유지)."""
from dataclasses import dataclass, field
from typing import Any

from security.policy import Policy


@dataclass
class SupervisorDeps:
    fast_llm: Any            # 라우팅·탐색 선택 등 짧은 단계용 저비용 모델
    quality_llm: Any         # 설명 작성용 모델 (로컬)
    mcp: Any                 # MCP stdio 클라이언트
    session_store: Any       # SQLite 세션 저장소 (Base 유지)
    version: str             # v2: 에이전트 그래프 | v1: LLM 없는 구조 출력
    policy: Policy = field(default_factory=Policy)
    cloud_llm: Any = None    # 클라우드 설명 모델 (게이트가 허용한 요청에만 사용). 없으면 None
    cloud_endpoint: str | None = None
    explore_mode: str = "agent"   # "agent": LLM이 열어볼 함수를 고른다 | "fixed": 코드가 전부 연다
