"""v1 — LLM 없는 구조 출력 모드 (Base의 legacy 선형 파이프라인 자리를 대체).

SUPERVISOR_VERSION=v1이면 모델을 전혀 부르지 않고 진입점 탐색 + 호출 체인만으로 답한다.
고객 환경에 모델이 없을 때, 또는 v2에서 문제가 생겼을 때 환경변수 하나로 돌아오는 지점이다.
"""
from langsmith import traceable

from backend.app.retrieval.entrypoint_search import search

from .deps import SupervisorDeps
from .retry import with_retry


@traceable(name="supervisor_v1_no_llm")
async def run_v1(deps: SupervisorDeps, query: str, repo: str, thread_id: str,
                 history: list[dict]) -> dict:
    events = [{"type": "Legacy", "note": "v1: LLM 없는 구조 출력"}]
    listing = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": repo})
    listing = listing[0] if isinstance(listing, tuple) else listing
    hits = search(query, listing["functions"])
    if not hits:
        return {"final": "질문에서 시작할 함수를 찾지 못했습니다.", "route": "no-llm",
                "mode": "not_found", "events": events, "cost_usd": 0.0}
    top = hits[0]
    chain = await with_retry(deps.mcp.call_tool, "get_chain",
                             {"repo": repo, "symbol": top["id"], "depth": deps.policy.max_depth})
    chain = chain[0] if isinstance(chain, tuple) else chain
    lines = [f"[구조 — 정적 분석으로 확인됨] 시작 함수: {top['qualname']}"]
    lines += [f"- {a} → {b}" for a, b in chain["pairs"]] or ["- (직접 호출이 확인되지 않았습니다)"]
    if chain["unresolved"]:
        lines.append("[분석이 따라가지 못한 호출 — 확인 안 됨]")
        lines += [f"- {u['caller']}: {u['text']} ({u['reason']})" for u in chain["unresolved"]]
    events += [{"type": "EntryPoint", "symbol": top["qualname"], "score": top["score"],
                "candidates": [h["qualname"] for h in hits]},
               {"type": "Finalize", "pipeline": "no-llm-v1"}]
    return {"final": "\n".join(lines), "route": "no-llm", "mode": "no-llm", "events": events,
            "cost_usd": 0.0, "structure": {"qualname": top["qualname"], "pairs": chain["pairs"],
                                           "unresolved": chain["unresolved"]}}
