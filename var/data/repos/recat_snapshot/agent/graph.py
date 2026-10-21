"""Recat 에이전트 그래프 (Base의 StateGraph 흐름을 유지하고 노드 내용을 교체).

gate → route → (chat_answer
               | locate → explore → explain → verify → (finalize | explain | fallback)
               | doc_locate → doc_check → doc_explain → doc_verify → finalize)

  gate        보안 게이트(코드): 민감도·정책으로 로컬/클라우드 경로와 트레이싱 허용 여부를 정한다
  route       질문이 코드 흐름 질문인지 판단 (LLM)
  locate      진입점 탐색 — 질문에서 시작 함수를 고른다 (여기에만 검색을 쓴다)
  explore     MCP get_callees/get_source로 호출을 따라간다. 열어볼 함수는 LLM이 고르되
              호출 횟수·깊이에는 코드로 상한을 둔다
  explain     설명과 호출 관계 주장 목록(claims)을 만든다
  verify      claims를 정적 분석 결과(MCP get_chain)와 코드로 대조한다 (Base의 길이·번호 검증 교체)
  fallback    검증을 끝내 통과하지 못하면 LLM 없는 구조 출력으로 대체한다
  doc_*       문서 경로: 문서 절 검색(MCP search_docs) → 문서의 호출 관계를 그래프와 대조(check_doc_section,
              결정적) → 설명(LLM, 대응 판단은 '추정') → 설명 속 호출 주장 검증. 실패하면 설명을 버린다
모든 노드는 SupervisorDeps를 partial로 주입받는 함수이며 @traceable로 LangSmith에 기록된다.
"""
import asyncio
import re
from functools import partial
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from retrieval.entrypoint_search import class_scope, search
from security.gate import decide, is_sensitive, tracing_allowed

from .deps import SupervisorDeps
from .fake_llm import FastLLM as _RuleLLM
from .llm_common import LLMUnavailable

_RULES = _RuleLLM("rules", 0.0)    # 모델 서버가 죽었을 때 쓰는 규칙 기반 라우터(트러블슈팅 ③)
from .retry import with_retry
from .verifier import names_in_text, verify


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    repo: str
    history: list[dict]
    request_sensitive: bool
    attested: bool
    sensitive: bool
    decision: str                 # "local" | "cloud"
    tracing: bool
    route: str
    entry_id: str
    entry_name: str
    entry_ids: list[str]          # 클래스 범위 질문이면 그 클래스의 메서드 전부, 아니면 [entry_id]
    entry_names: list[str]
    scope: str                    # "function" | "class"
    followup_of: str
    candidates: list[dict]
    structure: dict               # {qualname, pairs, unresolved, procedure}
    snippets: dict
    tool_calls: int
    chain: dict                   # MCP get_chain 결과 (검증의 정답 집합)
    explanation: str
    claims: list[dict]
    verdict: dict
    attempts: int
    feedback: str | None
    mode: str                     # "agent" | "fixed-no-llm" | "no-llm-fallback" | "not_found"
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]
    doc_hits: list[dict]
    doc_section: dict
    doc_check: dict
    doc_symbol: str
    llm_down: bool                # 모델 서버가 재시도 후에도 응답하지 않음
    named_missing: bool           # 질문에 적은 코드 이름이 저장소에 없음


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


def _ev(state: AgentState, *new: dict) -> list[dict]:
    return state.get("events", []) + list(new)


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    sensitive = is_sensitive(deps.policy, state["repo"], state.get("request_sensitive", False))
    decision = decide(deps.policy, deps.cloud_endpoint if deps.cloud_llm else None,
                      sensitive, state.get("attested", False))
    tracing = tracing_allowed(deps.policy, sensitive)
    return {"sensitive": sensitive, "decision": decision, "tracing": tracing,
            "events": _ev(state, {"type": "GateDecided", "decision": decision,
                                  "sensitive": sensitive, "tracing": tracing})}


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    down = []
    # 규칙이 먼저 정한다: 코드 이름·'호출/흐름/동작/어떻게' → trace, '문서/명세/기획서/N단계' → doc.
    # 작은 분류 모델(qwen3:0.6b)은 규칙이 '잡담'이라고 본 애매한 질문에만 쓴다(트러블슈팅 ⑩).
    rule, usage = _RULES.route(state["query"])
    result, by = rule, "rule"
    if rule["route"] == "chat":
        try:   # 동기 LLM 호출은 스레드로 — 이벤트 루프(다른 사용자 요청)를 막지 않는다(트러블슈팅 ①)
            result, usage = await asyncio.to_thread(deps.fast_llm.route, state["query"])
            by = "model"
        except LLMUnavailable as e:
            down = [{"type": "LLMUnavailable", "stage": "route", "detail": str(e)[:120]}]
    out = {"route": result["route"], "cost_usd": _cost(state, usage),
           "events": _ev(state, *down, {"type": "RouteDecided", "route": result["route"], "by": by})}
    if result["route"] == "chat" and not _followup_base(state):
        # 잡담으로 분류됐어도 등록된 문서와 충분히 겹치면 문서 질문으로 본다('남은 한계가 뭐야?')
        try:
            res, _ = await with_retry(deps.mcp.call_tool, "search_docs", {"repo": state["repo"], "query": state["query"]})
            if res["hits"] and res["hits"][0]["score"] >= DOC_FALLBACK_MIN_SCORE:
                out["route"] = "doc"
                out["events"] = out["events"] + [{"type": "RouteToDocs", "section": res["hits"][0]["title"]}]
                return out
        except Exception:
            pass                                       # 문서 검색이 안 되면 원래 분류대로
    prev = _followup_base(state) if out["route"] == "chat" else None
    if prev:
        # 'init이 아니라 전체를 보여줘'처럼 코드 이름 없이 직전 코드 질문을 고쳐 묻는 경우 —
        # 잡담으로 보내지 않고 직전 질문에 이어 붙여 다시 코드 흐름으로 처리한다(규칙 기반, LLM 없음).
        out.update(query=prev + " " + state["query"], route="trace", followup_of=prev,
                   events=out["events"] + [{"type": "FollowUp", "of": prev}])
    return out


_FOLLOWUP = re.compile(r"아니|말고|전체|전부|모두|다\s*보여|다시|더\s*자세히|그거|그것|그럼|그러면|나머지")
_CODE_NAME = re.compile(r"[A-Za-z_]+\.[A-Za-z_]+|[A-Z][a-z0-9]+[A-Z]|\(\)")


def _followup_base(state: AgentState) -> str | None:
    q = state["query"]
    if not _FOLLOWUP.search(q) or _CODE_NAME.search(q):
        return None
    for m in reversed(state.get("history", [])):
        if m.get("role") == "user":
            prev = m.get("content", "")
            return prev if _CODE_NAME.search(prev) else None
    return None


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    writer = get_stream_writer()
    try:
        reply, usage = await deps.fast_llm.chat(
            state["query"], state.get("history", []),
            on_token=lambda piece: writer({"type": "AnswerToken", "attempt": 0, "delta": piece}))
    except LLMUnavailable:
        reply, usage = ("지금은 대화 모델이 응답하지 않습니다. 코드 흐름 질문(함수·클래스 이름 포함)은 "
                        "정적 분석만으로도 답할 수 있으니 그렇게 물어봐 주세요."), {"usd": 0.0}
    return {"final": reply, "mode": "chat", "cost_usd": _cost(state, usage),
            "events": _ev(state, {"type": "Finalize", "pipeline": "chat-direct"})}


@traceable(name="locate_node")
async def locate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    listing, _ = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": state["repo"]})
    scope = class_scope(state["query"], listing["functions"])
    if scope:
        owner, methods = scope
        return {"entry_id": methods[0]["id"], "entry_name": owner, "scope": "class",
                "entry_ids": [m["id"] for m in methods], "entry_names": [m["qualname"] for m in methods],
                "candidates": methods,
                "events": _ev(state, {"type": "EntryPoint", "symbol": owner, "scope": "class", "score": None,
                                      "candidates": [m["qualname"] for m in methods]})}
    hits = search(state["query"], listing["functions"])
    # 질문에 'A.b'처럼 코드 이름을 적었는데 저장소에 그 이름이 없으면, 비슷한 함수로 대신 답하지 않는다
    # (예전: 'PaymentGateway.charge 흐름' → 토큰이 겹친 PaymentClient._post의 흐름을 답함)
    named = [n for n in names_in_text(state["query"]) if "." in n]
    if named and (not hits or hits[0]["score"] < 6):
        near = ", ".join(h["qualname"] for h in hits[:3]) or "없음"
        return {"mode": "not_found", "candidates": hits, "named_missing": True,
                "final": f"{', '.join(named)}은(는) 이 저장소에 없는 이름입니다. 비슷한 이름: {near}",
                "events": _ev(state, {"type": "EntryPointNotFound", "named": named,
                                      "near": [h["qualname"] for h in hits[:3]]})}
    if not hits:
        return {"mode": "not_found", "candidates": [],
                "final": "질문에서 시작할 함수를 찾지 못했습니다. 함수 이름이나 기능을 더 구체적으로 알려주세요.",
                "events": _ev(state, {"type": "EntryPointNotFound"})}
    top = hits[0]
    return {"entry_id": top["id"], "entry_name": top["qualname"], "candidates": hits, "scope": "function",
            "entry_ids": [top["id"]], "entry_names": [top["qualname"]],
            "events": _ev(state, {"type": "EntryPoint", "symbol": top["qualname"],
                                  "score": top["score"],
                                  "candidates": [h["qualname"] for h in hits]})}


@traceable(name="explore_node")
async def explore_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """호출을 따라간다. 상한: 호출 횟수(policy.max_tool_calls)와 깊이(policy.max_depth)."""
    repo, pol = state["repo"], deps.policy
    calls, retries, cost = 0, 0, state.get("cost_usd", 0.0)
    pairs: list[tuple[str, str]] = []
    unresolved: list[dict] = []
    snippets: dict[str, str] = {}
    events = list(state.get("events", []))
    entries = list(zip(state.get("entry_ids") or [state["entry_id"]],
                       state.get("entry_names") or [state["entry_name"]]))
    is_class = state.get("scope") == "class"
    visited = {sid for sid, _ in entries}
    frontier = list(entries)
    budget_hit = False

    async def read(sid: str, name: str):
        nonlocal calls, retries
        if calls >= pol.max_tool_calls:
            return
        src, r = (await with_retry(deps.mcp.call_tool, "get_source",
                                          {"repo": repo, "symbol": sid}))
        calls, retries = calls + 1, retries + r
        snippets[name] = src["source"]

    for sid, name in entries:
        await read(sid, name)
    for depth in range(1, pol.max_depth + 1):
        next_frontier: list[tuple[str, str]] = []
        for sid, name in frontier:
            if calls >= pol.max_tool_calls:
                budget_hit = True
                break
            res, r = (await with_retry(deps.mcp.call_tool, "get_callees",
                                              {"repo": repo, "symbol": sid}))
            calls, retries = calls + 1, retries + r
            events.append({"type": "ExploreStep", "depth": depth, "symbol": name,
                           "callees": len(res["callees"]),
                           "unresolved": sum(1 for u in res["unresolved"] if u["reason"] != "external")})
            for u in res["unresolved"]:
                if u["reason"] == "external":
                    continue   # 저장소 밖 호출은 분석의 빈틈이 아니다 (get_chain과 같은 기준)
                unresolved.append({"caller": name, **u})
            by_name = {c["qualname"]: c for c in res["callees"]}
            for c in res["callees"]:
                pairs.append((name, c["qualname"]))
            if not by_name:
                continue
            if deps.explore_mode == "agent":
                try:
                    chosen, usage = await asyncio.to_thread(deps.fast_llm.select_expansions,
                                                            state["query"], list(by_name))
                    cost = round(cost + usage["usd"], 6)
                except LLMUnavailable:
                    chosen = list(by_name)       # 모델이 없으면 코드가 전부 연다(안전한 쪽)
            else:
                chosen = list(by_name)           # fixed: 코드가 전부 연다
            for qn in chosen:
                c = by_name.get(qn)
                if c and c["id"] not in visited:
                    visited.add(c["id"])
                    next_frontier.append((c["id"], qn))
                    if not is_class:            # 클래스 범위는 메서드 본문만 읽고, 아래로는 호출만 따라간다(호출 상한 절약)
                        await read(c["id"], qn)
        frontier = next_frontier
        if not frontier or budget_hit:
            break
    if budget_hit:
        events.append({"type": "ToolBudgetReached", "limit": pol.max_tool_calls})

    procedure = ""
    try:
        prompt_server = await deps.mcp.find_prompt("code_trace_prompt")
        procedure = await deps.mcp.get_prompt(
            "code_trace_prompt", {"repo": repo, "question": state["query"]}, server=prompt_server)
    except Exception:
        pass   # 표준 절차 프롬프트를 못 가져와도 탐색 결과 자체는 유효하다

    structure = {"qualname": state["entry_name"], "pairs": sorted(set(pairs)),
                 "unresolved": unresolved, "procedure": procedure,
                 "scope": state.get("scope", "function"), "entries": [n for _, n in entries]}
    return {"structure": structure, "snippets": snippets, "tool_calls": calls,
            "cost_usd": cost,
            "events": events + [{"type": "ExploreDone", "tool_calls": calls, "retries": retries,
                                 "pairs": len(structure["pairs"]), "mode": deps.explore_mode}]}


def _pick_llm(state: AgentState, deps: SupervisorDeps, attempt: int):
    """에스컬레이션: 로컬 모델로 재시도하다 마지막 시도에서만, 게이트가 허용했을 때 클라우드로 올린다."""
    last = deps.policy.max_explain_retries + 1
    if attempt >= last and state.get("decision") == "cloud" and deps.cloud_llm is not None:
        return deps.cloud_llm, "cloud"
    return deps.quality_llm, "local"


@traceable(name="explain_node")
async def explain_node(state: AgentState, deps: SupervisorDeps) -> dict:
    attempt = state.get("attempts", 0) + 1
    llm, where = _pick_llm(state, deps, attempt)
    # 검증 전의 설명은 사용자에게 흘려보내지 않는다(틀린 주장이 화면에 남는 것을 막기 위해).
    try:
        out, usage = await llm.explain(
            state["query"], state["structure"], state.get("snippets", {}), attempt,
            state.get("feedback"))
    except LLMUnavailable as e:
        # 재시도 후에도 모델이 없다 — 정적 분석 결과는 이미 있으므로 구조만으로 답한다(fallback)
        return {"llm_down": True, "explanation": "", "claims": [], "attempts": attempt,
                "events": _ev(state, {"type": "LLMUnavailable", "stage": "explain", "detail": str(e)[:120]})}
    events = _ev(state, {"type": "Explained", "attempt": attempt, "backend": where,
                         "claims": len(out["claims"])})
    if where == "cloud":
        events.append({"type": "Escalated", "to": "cloud"})
    return {"explanation": out["explanation"], "claims": out["claims"], "attempts": attempt,
            "cost_usd": _cost(state, usage), "events": events}


@traceable(name="verify_node")
async def verify_node(state: AgentState, deps: SupervisorDeps) -> dict:
    chain = state.get("chain")
    if chain is None:
        # 정답 집합 = 시작 함수(클래스 범위면 메서드 전부)에서 출발한 호출 체인의 합집합
        chain = {"pairs": [], "unresolved": [], "known_symbols": []}
        for sid in state.get("entry_ids") or [state["entry_id"]]:
            part, _ = await with_retry(deps.mcp.call_tool, "get_chain",
                                       {"repo": state["repo"], "symbol": sid, "depth": deps.policy.max_depth})
            chain["pairs"] += [p for p in part["pairs"] if p not in chain["pairs"]]
            chain["unresolved"] += part["unresolved"]
            chain["known_symbols"] = part["known_symbols"]
    # 설명 본문도 검사한다: 본문에 적은 코드 이름이 저장소에 없으면 claims에 없더라도 오류(트러블슈팅 ⑥),
    # 설명이 비었거나 호출이 있는데 주장이 하나도 없으면 통과로 치지 않는다(트러블슈팅 ⑤)
    verdict = verify(state["claims"], chain, text=state.get("explanation", ""),
                     require_claims=bool(state["structure"]["pairs"]))
    feedback = None if verdict.ok else verdict.feedback()
    return {"chain": chain, "verdict": verdict.as_dict(), "feedback": feedback,
            "events": _ev(state, {"type": "Verification", "attempt": state["attempts"],
                                  **{k: verdict.as_dict()[k] for k in
                                     ("ok", "verified", "hallucinated", "wrong", "unverifiable")}})}


def _structure_text(structure: dict, chain: dict | None) -> str:
    pairs = structure["pairs"]
    lines = [f"- {a} → {b}" for a, b in pairs] or ["- (직접 호출이 확인되지 않았습니다)"]
    out = ["[구조 — 정적 분석으로 확인됨]"]
    if structure.get("scope") == "class":
        out.append(f"- 범위: {structure['qualname']} 클래스 전체 (메서드 {len(structure['entries'])}개: "
                   + ", ".join(structure["entries"]) + ")")
    out += lines
    if structure["unresolved"]:
        out.append("[분석이 따라가지 못한 호출 — 확인 안 됨]")
        out += [f"- {u['caller']}: {u['text']} ({u['reason']})" for u in structure["unresolved"]]
    return "\n".join(out)


@traceable(name="fallback_node")
async def fallback_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """검증을 끝내 통과하지 못했다 — LLM 설명은 버리고 분석 결과만으로 응답한다."""
    reason = "llm_unavailable" if state.get("llm_down") else "verification_failed"
    return {"mode": "no-llm-fallback", "explanation": "",
            "events": _ev(state, {"type": "Fallback", "reason": reason,
                                  "attempts": state.get("attempts", 0)})}


# ---- 문서 경로 ----

_VERDICT_TEXT = {"match": "✓ 일치", "indirect": "✓ 일치(간접)", "mismatch": "✗ 불일치",
                 "missing": "✗ 코드에 없는 이름", "unknown": "? 확인 안 됨"}


@traceable(name="doc_locate_node")
async def doc_locate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    res, _ = await with_retry(deps.mcp.call_tool, "search_docs", {"repo": state["repo"], "query": state["query"]})
    from_trace = state.get("mode") == "not_found"      # 코드 질문으로 봤는데 시작 함수가 없어서 넘어온 경우
    if from_trace:
        # 넘어온 경우엔 문서와 충분히 겹칠 때만 문서로 답한다(아무 절이나 붙잡지 않게)
        res = {**res, "hits": [h for h in res["hits"] if h["score"] >= DOC_FALLBACK_MIN_SCORE]}
    events = _ev(state, {"type": "DocSearch", "sections": res["total_sections"],
                         "hits": [h["title"] for h in res["hits"]], "fallback": from_trace})
    if not res["hits"] and from_trace:
        return {"doc_hits": [], "events": events}       # 원래의 '시작 함수를 찾지 못했습니다'를 그대로 낸다
    if not res["hits"]:
        msg = ("이 저장소에는 색인된 문서가 없습니다. 저장소 안에 md·txt·rst 문서를 두고 관리자 화면에서 재분석하세요."
               if not res["total_sections"] else
               "질문과 맞는 문서 절을 찾지 못했습니다. 문서의 제목이나 단계 번호를 함께 적어 주세요.")
        return {"mode": "doc_not_found", "final": msg, "doc_hits": [], "tool_calls": 1,
                "events": events + [{"type": "DocNotFound"}]}
    # 질문이 함수 이름을 직접 적었으면(점수 6 이상) 그 함수와 문서 절의 연결도 확인한다
    listing, _ = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": state["repo"]})
    named = [h for h in search(state["query"], listing["functions"], top_k=1) if h["score"] >= 6]
    return {"doc_hits": res["hits"], "doc_symbol": named[0]["id"] if named else "", "tool_calls": 2,
            **({"mode": None, "route": "doc"} if from_trace else {}),
            "events": events + [{"type": "DocSection", "section": res["hits"][0]["title"],
                                 "file": res["hits"][0]["file"],
                                 "symbol": named[0]["qualname"] if named else None}]}


@traceable(name="doc_check_node")
async def doc_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    sid = state["doc_hits"][0]["id"]
    sec, _ = await with_retry(deps.mcp.call_tool, "get_doc_section", {"repo": state["repo"], "section_id": sid})
    args = {"repo": state["repo"], "section_id": sid}
    if state.get("doc_symbol"):
        args["symbol"] = state["doc_symbol"]
    chk, _ = await with_retry(deps.mcp.call_tool, "check_doc_section", args)
    return {"doc_section": sec, "doc_check": chk, "tool_calls": state.get("tool_calls", 0) + 2,
            "events": _ev(state, {"type": "DocCheck", **chk["counts"],
                                  "relation": (chk.get("relation") or {}).get("relation")})}


@traceable(name="doc_explain_node")
async def doc_explain_node(state: AgentState, deps: SupervisorDeps) -> dict:
    llm, where = deps.quality_llm, "local"     # 문서 경로는 재시도가 없으므로 클라우드로 올리지 않는다
    try:
        out, usage = await llm.explain_doc(state["query"], state["doc_section"], state["doc_check"])
    except LLMUnavailable as e:   # 문서 대조 결과(결정적)는 그대로 두고 해석만 뺀다
        return {"explanation": "", "claims": [], "attempts": 1, "mode": "doc-no-llm", "llm_down": True,
                "events": _ev(state, {"type": "LLMUnavailable", "stage": "doc_explain", "detail": str(e)[:120]})}
    return {"explanation": out["explanation"], "claims": out["claims"], "attempts": 1,
            "cost_usd": _cost(state, usage),
            "events": _ev(state, {"type": "Explained", "attempt": 1, "backend": where,
                                  "claims": len(out["claims"])})}


@traceable(name="doc_verify_node")
async def doc_verify_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """설명이 '호출한다'고 말한 관계를 호출 체인과 대조한다. 오류가 하나라도 있으면 설명을 버린다(재시도 없음)."""
    chain = {"pairs": [], "unresolved": [], "known_symbols": []}
    listing, _ = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": state["repo"]})
    chain["known_symbols"] = [f["qualname"] for f in listing["functions"]]
    for caller in sorted({c.get("caller", "") for c in state.get("claims", [])}):
        if caller not in chain["known_symbols"]:
            continue
        try:
            part, _ = await with_retry(deps.mcp.call_tool, "get_chain",
                                       {"repo": state["repo"], "symbol": caller, "depth": 1})
        except Exception:
            continue
        chain["pairs"] += part["pairs"]
        chain["unresolved"] += part["unresolved"]
    verdict = verify(state.get("claims", []), chain)
    d = verdict.as_dict()
    out = {"verdict": d, "events": _ev(state, {"type": "Verification", "attempt": 1,
                                                **{k: d[k] for k in ("ok", "verified", "hallucinated", "wrong", "unverifiable")}})}
    if not verdict.ok:
        out["explanation"] = ""
        out["mode"] = "doc-no-llm"
    return out


def _doc_text(state: AgentState) -> str:
    sec, chk = state["doc_section"], state["doc_check"]
    excerpt = "\n".join(f"> {l}" for l in sec["text"].splitlines() if l.strip())
    parts = [f"[문서 근거 — {sec['file']} · {sec['title']} (문서 내용이며 검증 대상 아님)]\n{excerpt}"]
    rows = [f"- {_VERDICT_TEXT[c['verdict']]}: {c['doc']} — {c['detail']}" for c in chk["claims"]]
    parts.append("[코드 대조 — 문서의 호출 관계를 정적 분석과 대조]\n" +
                 ("\n".join(rows) if rows else "- 이 절에는 코드 이름으로 적힌 호출 관계가 없습니다"))
    rel = chk.get("relation")
    if rel:
        mark = {"direct": "✓", "linked": "✓", "none": "?"}[rel["relation"]]
        parts.append(f"[질문한 함수와의 연결 — 정적 분석으로 확인]\n- {mark} {rel['symbol']}: {rel['detail']}")
    if state.get("explanation"):
        parts.append("[해석 — 설명 모델의 판단, 대응 여부는 추정이며 검증되지 않음]\n" + state["explanation"])
    else:
        parts.append("[해석 없음 — 모델 서버가 응답하지 않아 문서와 대조 결과만 제공합니다]" if state.get("llm_down") else
                     "[해석 없음 — 설명 검증을 통과하지 못해 문서와 대조 결과만 제공합니다]")
    return "\n\n".join(parts)


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    if state.get("doc_check"):
        mode = state.get("mode") or "doc"
        final = _doc_text(state)
        get_stream_writer()({"type": "AnswerToken", "attempt": 0, "delta": final})
        return {"final": final, "mode": mode, "events": _ev(state, {"type": "Finalize", "pipeline": mode})}
    if state.get("mode") in ("chat", "not_found", "doc_not_found") or "structure" not in state:
        return {"final": state["final"],
                "events": _ev(state, {"type": "Finalize", "pipeline": state.get("mode", "")})}
    mode = state.get("mode") or "agent"
    parts = [_structure_text(state["structure"], state.get("chain"))]
    if state.get("explanation"):
        parts.append("[해석 — 설명 모델의 서술, 의도·이유는 검증되지 않음]\n" + state["explanation"])
    elif mode == "no-llm-fallback":
        parts.append("[해석 없음 — 모델 서버가 응답하지 않아 정적 분석 결과만 제공합니다]" if state.get("llm_down") else
                     "[해석 없음 — 설명 검증을 통과하지 못해 구조만 제공합니다]")
    final = "\n\n".join(parts)
    # 검증을 마친 최종 응답만 스트림으로 내보낸다.
    get_stream_writer()({"type": "AnswerToken", "attempt": 0, "delta": final})
    return {"final": final, "mode": mode,
            "events": _ev(state, {"type": "Finalize", "pipeline": mode})}


def _after_route(state: AgentState) -> str:
    return {"chat": "chat_answer", "doc": "doc_locate"}.get(state["route"], "locate")


def _after_doc_locate(state: AgentState) -> str:
    return "finalize" if state.get("mode") in ("doc_not_found", "not_found") else "doc_check"


DOC_FALLBACK_MIN_SCORE = 3.0


def _after_locate(state: AgentState) -> str:
    # 시작 함수를 못 찾으면 바로 끝내지 않고 문서에서 한 번 더 찾는다
    # ('트러블슈팅에서 MCP가 죽으면?'처럼 '문서'라는 말 없이 문서 내용을 묻는 경우)
    # 단, 사용자가 코드 이름을 콕 집었는데 없는 경우는 문서로 돌리지 않는다 — 답은 '없는 이름'이다
    if state.get("mode") != "not_found":
        return "explore"
    return "finalize" if state.get("named_missing") else "doc_locate"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)
    for name, fn in [("gate", gate_node), ("route", route_node), ("chat_answer", chat_answer_node),
                     ("locate", locate_node), ("explore", explore_node), ("explain", explain_node),
                     ("verify", verify_node), ("fallback", fallback_node),
                     ("finalize", finalize_node), ("doc_locate", doc_locate_node),
                     ("doc_check", doc_check_node), ("doc_explain", doc_explain_node),
                     ("doc_verify", doc_verify_node)]:
        g.add_node(name, node(fn))
    max_retries = deps.policy.max_explain_retries

    def after_verify(state: AgentState) -> str:
        if state["verdict"]["ok"]:
            return "finalize"
        # attempts: 1(첫 설명) + max_retries(재설명) 까지 허용
        return "explain" if state["attempts"] < 1 + max_retries else "fallback"

    g.set_entry_point("gate")
    g.add_edge("gate", "route")
    g.add_conditional_edges("route", _after_route, {"chat_answer": "chat_answer", "locate": "locate",
                                                    "doc_locate": "doc_locate"})
    g.add_conditional_edges("doc_locate", _after_doc_locate, {"finalize": "finalize", "doc_check": "doc_check"})
    g.add_edge("doc_check", "doc_explain")
    g.add_edge("doc_explain", "doc_verify")
    g.add_edge("doc_verify", "finalize")
    g.add_edge("chat_answer", END)
    g.add_conditional_edges("locate", _after_locate, {"doc_locate": "doc_locate", "explore": "explore",
                                                      "finalize": "finalize"})
    g.add_edge("explore", "explain")
    g.add_conditional_edges("explain", lambda s: "fallback" if s.get("llm_down") else "verify",
                            {"fallback": "fallback", "verify": "verify"})
    g.add_conditional_edges("verify", after_verify,
                            {"finalize": "finalize", "explain": "explain", "fallback": "fallback"})
    g.add_edge("fallback", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
