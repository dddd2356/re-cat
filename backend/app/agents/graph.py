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

from backend.app.retrieval.entrypoint_search import class_scope, search
from backend.app.security.gate import decide, is_sensitive, tracing_allowed

from .deps import SupervisorDeps
from .routing import route_by_rule
from .llm_common import LLMUnavailable

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
    doc_all: bool
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
    down, candidates = [], []
    # 라우팅 전에 등록 문서의 후보를 찾고 LLM에게 함께 준다. '트러블 슈팅'처럼 문서라는
    # 단어가 없는 질문도 제목·본문과 대조해 문서-코드 검증 경로로 보낼 수 있어야 한다.
    try:
        docs, _ = await with_retry(deps.mcp.call_tool, "search_docs",
                                   {"repo": state["repo"], "query": state["query"]})
        candidates = docs["hits"]
    except Exception:
        candidates = []             # 문서 검색 장애가 코드 질문 자체를 막지는 않는다
    rule, usage = route_by_rule(state["query"])
    result, by = rule, "rule-fallback"
    try:   # 동기 LLM 호출은 스레드로 — 이벤트 루프(다른 사용자 요청)를 막지 않는다(트러블슈팅 ①)
        result, usage = await asyncio.to_thread(deps.fast_llm.route, state["query"], candidates)
        by = "model"
        # 규칙이 '잡담'이 아니라고 확신했으면(doc 또는 trace) 모델이 뭐라 답해도 규칙이 이긴다 —
        # 모델에게는 규칙이 '잡담'으로 본 애매한 질문만 맡긴다. 그렇지 않으면 "_doc_files 호출 흐름"처럼
        # 코드 이름+호출 신호로 trace가 확실한 질문도, 검색된 문서 후보에 끌려 모델이 doc으로
        # 돌려버리는 경우를 막지 못한다.
        if rule["route"] != "chat" and result["route"] != rule["route"]:
            result, by = rule, "rule-fallback"
        # 모델이 잡담으로 오판해도, 등록 문서에서 충분히 관련성 높은 절이 검색됐으면
        # 문서-코드 대조를 우선한다. 키워드 목록이 아니라 실제 등록 문서의 검색 근거를
        # 쓰는 안전망이다.
        elif (result["route"] == "chat" and rule["route"] == "chat" and candidates
                and candidates[0]["score"] >= DOC_FALLBACK_MIN_SCORE):
            result, by = {"route": "doc"}, "retrieval-fallback"
    except LLMUnavailable as e:
        down = [{"type": "LLMUnavailable", "stage": "route", "detail": str(e)[:120]}]
    out = {"route": result["route"], "cost_usd": _cost(state, usage),
           "events": _ev(state, *down, {"type": "RouteDecided", "route": result["route"], "by": by})}
    if candidates:
        out["events"] += [{"type": "DocumentCandidates", "sections": [h["title"] for h in candidates]}]
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


# 되묻기(clarify) 기준 — 둘 다 "점수가 뭘 뜻하는지"에서 뽑았다(12문항을 보고 거꾸로 맞춘 숫자가 아니다).
# search()의 점수는 이름 일치(10/6, 압도적으로 크다) + 토큰 겹침(겹치는 단어 하나당 0.5)이다.
#   WEAK_TOP_SCORE: 이름 일치 없이 토큰 겹침만으로 올라왔는데 겹친 단어가 1개(0.5점) 이하면,
#   "어떻게"·"무엇을" 같은 말 하나가 우연히 겹친 것과 구별할 수 없다 → 1.0(토큰 2개) 미만이면 약하다.
#   CLOSE_GAP: 1등이 2등보다 겹치는 단어를 단 하나도 더 가지지 못했으면(0.5점 미만 차이) 그 둘을
#   가를 근거가 없다 — 1등이 2등보다 단어 하나(0.5점)라도 더 겹쳤으면 그 단어가 결정한 것으로 본다.
#   (처음엔 2.0/1.0으로 더 크게 잡았는데, 독스트링이 짧은 샘플 저장소에서는 정답조차 점수가 작아
#   근거 있는 정답까지 되묻게 됐다 — 짧은 문장에서는 "단어 몇 개 겹쳤나"의 절대 개수가 작을 수밖에
#   없으므로, 점수의 의미(겹친 단어 개수)에 맞게 기준을 다시 "최소 단위 1개" 수준으로 낮췄다.)
WEAK_TOP_SCORE = 1.0
CLOSE_GAP = 0.5


def _should_clarify(hits: list[dict]) -> bool:
    if not hits:
        return False
    weak_top = hits[0]["score"] < WEAK_TOP_SCORE
    close_gap = len(hits) >= 2 and (hits[0]["score"] - hits[1]["score"]) < CLOSE_GAP
    return weak_top or close_gap


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
    # 질의 확장: 한국어 질문을 fast 모델로 영어 키워드 3~6개로 바꿔 검색 토큰에 더한다(코드 이름은 대부분
    # 영어라, 한국어 질문과 겹칠 말이 많지 않다). 모델이 없거나 실패하거나 빈 응답이면 기존 키워드 검색만 쓴다.
    expanded: list[str] = []
    cost = state.get("cost_usd", 0.0)
    try:
        expanded, usage = await asyncio.to_thread(deps.fast_llm.expand_query, state["query"])
        cost = _cost(state, usage)
    except Exception:
        expanded = []
    hits = search(state["query"], listing["functions"], extra_terms=expanded)
    # 질문에 'A.b'처럼 코드 이름을 적었는데 저장소에 그 이름이 없으면, 비슷한 함수로 대신 답하지 않는다
    # (예전: 'PaymentGateway.charge 흐름' → 토큰이 겹친 PaymentClient._post의 흐름을 답함)
    named = [n for n in names_in_text(state["query"]) if "." in n]
    if named and (not hits or hits[0]["score"] < 6):
        near = ", ".join(h["qualname"] for h in hits[:3]) or "없음"
        return {"mode": "not_found", "candidates": hits, "named_missing": True, "cost_usd": cost,
                "final": f"{', '.join(named)}은(는) 이 저장소에 없는 이름입니다. 비슷한 이름: {near}",
                "events": _ev(state, {"type": "EntryPointNotFound", "named": named,
                                      "near": [h["qualname"] for h in hits[:3]]})}
    if not hits:
        return {"mode": "not_found", "candidates": [], "cost_usd": cost,
                "final": "질문에서 시작할 함수를 찾지 못했습니다. 함수 이름이나 기능을 더 구체적으로 알려주세요.",
                "events": _ev(state, {"type": "EntryPointNotFound"})}
    if _should_clarify(hits):
        top3 = hits[:3]
        names = [h["qualname"] for h in top3]
        return {"mode": "clarify", "candidates": top3, "cost_usd": cost,
                "final": "이 중 어느 함수를 말씀하시나요?\n" + "\n".join(f"- {n}" for n in names),
                "events": _ev(state, {"type": "Clarify", "candidates": names,
                                      "scores": [h["score"] for h in top3]})}
    top = hits[0]
    return {"entry_id": top["id"], "entry_name": top["qualname"], "candidates": hits, "scope": "function",
            "entry_ids": [top["id"]], "entry_names": [top["qualname"]], "cost_usd": cost,
            "events": _ev(state, {"type": "EntryPoint", "symbol": top["qualname"],
                                  "score": top["score"],
                                  "candidates": [h["qualname"] for h in hits]})}


@traceable(name="explore_node")
async def explore_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """호출을 따라간다. 상한: 호출 횟수(policy.max_tool_calls)와 깊이(policy.max_depth)."""
    repo, pol = state["repo"], deps.policy
    calls, retries, cost = 0, 0, state.get("cost_usd", 0.0)
    pairs: list[tuple[str, str]] = []
    pair_ids: list[tuple[str, str]] = []
    unresolved: list[dict] = []
    snippets: dict[str, str] = {}
    events = list(state.get("events", []))
    entries = list(zip(state.get("entry_ids") or [state["entry_id"]],
                       state.get("entry_names") or [state["entry_name"]]))
    refs: dict[str, dict] = {sid: {"id": sid, "label": name} for sid, name in entries}
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
                pair_ids.append((sid, c["id"]))
                refs.setdefault(c["id"], {"id": c["id"], "label": c["qualname"], "file": c.get("file", "")})
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
                 "pair_ids": sorted(set(pair_ids)), "symbol_refs": list(refs.values()),
                 "entry_ids": [sid for sid, _ in entries],
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


def _unresolved_line(u: dict) -> str:
    if u.get("reason") == "candidates" and u.get("candidates"):
        return f"- {u['caller']}: {u['text']} → {' 또는 '.join(u['candidates'])} (런타임에 결정)"
    if u.get("reason") == "builtin_name_collision":
        return f"- {u['caller']}: {u['text']} (내장 메서드 이름과 겹침 — 저장소 안 메서드일 수 있음)"
    return f"- {u['caller']}: {u['text']} ({u['reason']})"


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
        out += [_unresolved_line(u) for u in structure["unresolved"]]
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


def _unique_named_symbol_ids(functions: list[dict], names: list[str]) -> list[str]:
    """이름이 유일하게 해석되는 경우에만 MCP용 심볼 ID를 돌려준다.

    문서는 사람이 읽는 qualname을 담지만, 업로드 저장소 안에 사본·벤더 코드가 있으면
    같은 qualname이 여럿일 수 있다. 그때 이름으로 MCP를 호출하거나 첫 항목을 고르면
    다른 파일을 근거로 삼을 위험이 있으므로, 모호한 이름은 코드 근거에서 제외한다.
    """
    by_name: dict[str, list[str]] = {}
    for function in functions:
        by_name.setdefault(function["qualname"], []).append(function["id"])
    return [by_name[name][0] for name in names if len(by_name.get(name, [])) == 1]


@traceable(name="doc_locate_node")
async def doc_locate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    res, _ = await with_retry(deps.mcp.call_tool, "search_docs", {"repo": state["repo"], "query": state["query"]})
    # '트러블슈팅 모든 것'처럼 한 문서 전체를 요청하면 검색 1위 절(흔히 README 요약)만
    # 답하지 않는다. 제목이 직접 맞는 원문 파일을 우선하고 그 파일의 모든 절을 넘긴다.
    all_requested = bool(re.search(r"전체|모든|전부|다\s*설명", state["query"]))
    if all_requested:
        # 검색 상위 3개에 원문이 없을 수 있으므로 카탈로그 전체에서 제목·파일명을 찾는다.
        # 공백을 없애 '트러블 슈팅'과 '트러블슈팅'을 같은 문서 이름으로 취급한다.
        catalog, _ = await with_retry(deps.mcp.call_tool, "list_doc_sections", {"repo": state["repo"]})
        preferred = next((h for h in catalog["hits"] if "troubleshooting" in h["file"].lower()), None)
        if preferred is None:
            preferred = next((h for h in catalog["hits"]
                              if "트러블슈팅" in re.sub(r"\s+", "", h["title"]).lower()), None)
        if preferred is None and res["hits"]:
            preferred = res["hits"][0]
        if preferred is not None:
            all_sections, _ = await with_retry(deps.mcp.call_tool, "list_doc_sections",
                                                {"repo": state["repo"], "file": preferred["file"]})
            if all_sections["hits"]:
                res = {**res, "hits": all_sections["hits"]}
    from_trace = state.get("mode") == "not_found"      # 코드 질문으로 봤는데 시작 함수가 없어서 넘어온 경우
    if from_trace and not all_requested:
        # 넘어온 경우엔 문서와 충분히 겹칠 때만 문서로 답한다(아무 절이나 붙잡지 않게)
        res = {**res, "hits": [h for h in res["hits"] if h["score"] >= DOC_FALLBACK_MIN_SCORE]}
    events = _ev(state, {"type": "DocSearch", "sections": res["total_sections"],
                         "hits": [h["title"] for h in res["hits"]], "fallback": from_trace,
                         "retrieval": res.get("retrieval", {"method":"keyword"})})
    if not res["hits"] and from_trace:
        return {"doc_hits": [], "events": events}       # 원래의 '시작 함수를 찾지 못했습니다'를 그대로 낸다
    if not res["hits"]:
        msg = ("이 저장소에는 색인된 문서가 없습니다. 저장소 안에 md·txt·rst 문서를 두고 관리자 화면에서 재분석하세요."
               if not res["total_sections"] else
               "질문과 맞는 문서 절을 찾지 못했습니다. 문서의 제목이나 단계 번호를 함께 적어 주세요.")
        return {"mode": "doc_not_found", "final": msg, "doc_hits": [], "tool_calls": 1,
                "events": events + [{"type": "DocNotFound"}]}
    # 전체 문서 요청은 문서 내용 요약이다. 문서 안의 예시 함수명을 질문한 코드로 오인하지 않는다.
    named = []
    if not all_requested:
        mentioned_names = names_in_text(state["query"])
        if mentioned_names:
            listing, _ = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": state["repo"]})
            named = [h for h in search(state["query"], listing["functions"], top_k=1)
                     if h["score"] >= 6 and h["qualname"] in mentioned_names]
    return {"doc_hits": res["hits"], "doc_symbol": named[0]["id"] if named else "", "doc_all": all_requested, "tool_calls": 2,
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
    if state.get("doc_all"):
        chk = {"claims": [], "counts": {k: 0 for k in ("match", "indirect", "mismatch", "missing", "unknown")}}
    else:
        chk, _ = await with_retry(deps.mcp.call_tool, "check_doc_section", args)
    # 검색 근거와 실제 소스를 함께 전달한다. 문서의 호출 주장을 코드의 사실로 승격하지 않는다.
    additional = []
    for hit in state["doc_hits"][1:]:
        other, _ = await with_retry(deps.mcp.call_tool, "get_doc_section", {"repo":state["repo"],"section_id":hit["id"]})
        additional.append(other)
    sec["additional_sections"] = additional
    sec["document_collection"] = state.get("doc_all", False)
    listing = {"functions": []}
    if not state.get("doc_all"):
        listing, _ = await with_retry(deps.mcp.call_tool,"list_functions",{"repo":state["repo"]})
    # doc_symbol은 locate 단계에서 이미 고른 고유 ID다. 문서의 mentions는 이름이므로
    # 유일하게 매핑되는 것만 ID로 바꿔 뒤의 get_source/get_chain에 전달한다.
    requested = [state.get("doc_symbol", "")]
    if state.get("doc_symbol"):
        for document in [sec] + additional:
            requested.extend(_unique_named_symbol_ids(listing["functions"], document.get("mentions", [])))
    evidence = []
    seen = set()
    for symbol in requested:
        if not symbol or symbol in seen or len(evidence)>=3:
            continue
        seen.add(symbol)
        try:
            source, _ = await with_retry(deps.mcp.call_tool,"get_source",{"repo":state["repo"],"symbol":symbol})
            chain, _ = await with_retry(deps.mcp.call_tool,"get_chain",{"repo":state["repo"],"symbol":symbol,"depth":1})
        except Exception:
            continue  # 확인 못 한 코드를 다른 함수로 대체하지 않는다.
        evidence.append({"id":source["symbol"],"symbol":chain["qualname"],"file":source["file"],"start_line":source["start_line"],
                         "source":source["source"],"pairs":chain["pairs"],"unresolved":chain["unresolved"]})
    sec["code_evidence"] = evidence
    chk["code_evidence_refs"] = [{"id": e["id"], "label": e["symbol"], "file": e["file"]} for e in evidence]
    return {"doc_section": sec, "doc_check": chk, "tool_calls": state.get("tool_calls",0)+3+len(additional)+2*len(evidence),
            "events": _ev(state, {"type":"DocCheck", **chk["counts"],
                                  "relation":(chk.get("relation") or {}).get("relation"),
                                  "code_evidence":[e["symbol"] for e in evidence]})}



@traceable(name="doc_explain_node")
async def doc_explain_node(state: AgentState, deps: SupervisorDeps) -> dict:
    llm, where = deps.quality_llm, "local"     # 문서 경로는 재시도가 없으므로 클라우드로 올리지 않는다
    try:
        out, usage = await llm.explain_doc(state["query"], state["doc_section"], state["doc_check"])
    except LLMUnavailable as e:   # 문서 대조 결과(결정적)는 그대로 두고 해석만 뺀다
        return {"explanation": "", "claims": [], "attempts": 1, "mode": "doc-no-llm", "llm_down": True,
                "events": _ev(state, {"type": "LLMUnavailable", "stage": "doc_explain", "detail": str(e)[:120]})}
    # 전체 문서 요약은 호출 관계를 주장하는 작업이 아니다. 문서 속 예시 코드가 claims로
    # 섞여 설명 전체가 버려지는 것을 막는다.
    claims = [] if state.get("doc_all") else out["claims"]
    return {"explanation": out["explanation"], "claims": claims, "attempts": 1,
            "cost_usd": _cost(state, usage),
            "events": _ev(state, {"type": "Explained", "attempt": 1, "backend": where,
                                  "claims": len(out["claims"])})}


@traceable(name="doc_verify_node")
async def doc_verify_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """설명이 '호출한다'고 말한 관계를 호출 체인과 대조한다. 오류가 하나라도 있으면 설명을 버린다(재시도 없음)."""
    if state.get("doc_all"):
        verdict = {"ok": True, "verified": 0, "hallucinated": 0, "wrong": 0,
                   "unverifiable": 0, "empty": False, "errors": [], "unverifiable_claims": []}
        return {"verdict": verdict,
                "events": _ev(state, {"type": "Verification", "attempt": 1, "ok": True,
                                      "verified": 0, "hallucinated": 0, "wrong": 0, "unverifiable": 0})}
    chain = {"pairs": [], "unresolved": [], "known_symbols": []}
    listing, _ = await with_retry(deps.mcp.call_tool, "list_functions", {"repo": state["repo"]})
    chain["known_symbols"] = [f["qualname"] for f in listing["functions"]]
    unique_ids = _unique_named_symbol_ids(listing["functions"], chain["known_symbols"])
    # claims는 모델이 사람이 읽는 qualname으로 낸다. 같은 이름이 여러 개면 호출 관계를
    # 단정할 수 없으므로 검증 근거로 쓰지 않는다(임의의 첫 파일을 선택하지 않는다).
    unique_id_by_name = {
        function["qualname"]: function["id"]
        for function in listing["functions"]
        if function["id"] in unique_ids
    }
    for caller in sorted({c.get("caller", "") for c in state.get("claims", [])}):
        symbol_id = unique_id_by_name.get(caller)
        if not symbol_id:
            continue
        try:
            part, _ = await with_retry(deps.mcp.call_tool, "get_chain",
                                       {"repo": state["repo"], "symbol": symbol_id, "depth": 1})
        except Exception:
            continue
        chain["pairs"] += part["pairs"]
        chain["unresolved"] += part["unresolved"]
    # 문서에 인용된 낡은/외부 이름과 코드 호출 주장을 구분한다.
    # 호출 주장은 원래 기준으로 전부 검증하고, 문장 전체의 사실성은 미검증 해석으로 표시한다.
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
    if state.get("doc_all"):
        parts = []
        if state.get("explanation"):
            parts.append("[전체 문서 요약 — AI 서술]\n" + state["explanation"])
        sections = [sec] + sec.get("additional_sections", [])
        for document in sections:
            parts.append(f"[문서 원문 — {document['file']} · {document['title']} · {document['line']}행]\n" +
                         "\n".join(f"> {line}" for line in document["text"].splitlines() if line.strip()))
        if not state.get("explanation"):
            parts.insert(0, "[요약을 만들지 못했습니다 — 모델 서버가 응답하지 않아 전체 원문을 표시합니다]")
        return "\n\n".join(parts)
    excerpt = "\n".join(f"> {l}" for l in sec["text"].splitlines() if l.strip())
    parts = []
    if state.get("doc_all") and state.get("explanation"):
        parts.append("[전체 문서 요약 — AI 서술]\n" + state["explanation"])
    parts.append(f"[문서 근거 — {sec['file']} · {sec['title']} (문서 내용이며 검증 대상 아님)]\n{excerpt}")
    rows = [f"- {_VERDICT_TEXT[c['verdict']]}: {c['doc']} — {c['detail']}" for c in chk["claims"]]
    parts.append("[코드 대조 — 문서의 호출 관계를 정적 분석과 대조]\n" +
                 ("\n".join(rows) if rows else "- 이 절에는 코드 이름으로 적힌 호출 관계가 없습니다"))
    rel = chk.get("relation")
    if rel:
        mark = {"direct": "✓", "linked": "✓", "none": "?"}[rel["relation"]]
        parts.append(f"[질문한 함수와의 연결 — 정적 분석으로 확인]\n- {mark} {rel['symbol']}: {rel['detail']}")
    for other in sec.get("additional_sections", []):
        parts.append(f"[추가 문서 근거 — {other['file']} · {other['title']}]\n" + other["text"])
    if sec.get("code_evidence"):
        parts.append("[코드 근거 — 실제 소스·직접 호출]\n" + "\n".join(
            f"- {e['symbol']} ({e['file']}:{e['start_line']}): " +
            (", ".join(f"{a} → {b}" for a,b in e["pairs"]) or "확정된 직접 호출 없음")
            for e in sec["code_evidence"]))
    if state.get("explanation") and not state.get("doc_all"):
        parts.append("[해석 — 설명 모델의 판단, 대응 여부는 추정이며 검증되지 않음]\n" + state["explanation"])
    elif not state.get("explanation"):
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
    if state.get("mode") in ("chat", "not_found", "doc_not_found", "clarify") or "structure" not in state:
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
    # 되묻기(clarify)는 탐색하지 않고 바로 끝낸다 — 후보 중 어느 것인지 모르는 채로 호출을 따라가면
    # (이전 루틴처럼) 틀린 함수에 대한 확신에 찬 답이 나온다.
    if state.get("mode") == "clarify":
        return "finalize"
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
