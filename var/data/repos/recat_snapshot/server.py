"""Recat FastAPI 백엔드 (Base server.py 구조 유지: lifespan 싱글턴, SSE, 세션, 버전 토글).

Base에서 그대로 쓴 것 : lifespan에서 Agent 번들 1회 생성, /chat, /chat/stream(SSE), SQLite 세션,
                       SUPERVISOR_VERSION 토글, 요청당 LangSmith 루트 run
추가한 것           : Bearer 키 인증(401), 저장소 허용 목록(403), 보안 게이트에 따른 트레이싱 차단,
                       /repos, /flows/{repo}, /audit, 감사 로그, Pydantic 스키마 확장
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import uuid

from observability.langsmith_feedback import FeedbackSender, has_key, run_url, scores_from
from security import registry
from security.policy import load_policy

_POLICY = registry.apply(load_policy())   # policy.yaml + 관리자가 등록한 저장소(더하기만)
# 입출력 숨김은 LangSmith 클라이언트가 만들어지기 전에 환경변수로 걸어 둔다.
if _POLICY.hide_inputs_outputs:
    os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
    os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")
if not _POLICY.langsmith_enabled:
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from langsmith import traceable, tracing_context
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.supervisor_v1 import run_v1
from security import audit
from security.auth import require_admin, require_key, require_repo
from security.gate import is_sensitive, tracing_allowed, tracing_decision, tracing_endpoint

CREATION_COUNT = 0   # 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str = "default"
    repo: str = "sample_py"
    sensitive: bool = False     # 요청 단위로 민감 처리를 강제 (저장소 정책과 OR)
    attested: bool = False      # 고객이 외부 전송을 명시적으로 확인했는가


def _build_llms():
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬). CLOUD_BACKEND=openai면 클라우드 설명 모델도 만든다."""
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"))
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"))
    else:
        fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.0)
        quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.0)
    cloud, endpoint = None, None
    if os.environ.get("CLOUD_BACKEND") == "openai":
        from agent.openai_llm import OpenAIQualityLLM
        cloud = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"), 0.005)
        endpoint = cloud.endpoint()
    return backend, fast, quality, cloud, endpoint


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    CREATION_COUNT += 1
    mcp = MCPClient(flaky_first_tool_call=os.environ.get("RECAT_FLAKY") == "1")
    await mcp.start()    # MCP subprocess는 서버 수명 동안 딱 1개
    backend, fast_llm, quality_llm, cloud_llm, cloud_endpoint = _build_llms()
    app.state.llm_backend = backend
    deps = SupervisorDeps(
        fast_llm=fast_llm, quality_llm=quality_llm, mcp=mcp,
        session_store=SqliteSessionStore(),
        version=os.environ.get("SUPERVISOR_VERSION", "v2"),
        policy=_POLICY, cloud_llm=cloud_llm, cloud_endpoint=cloud_endpoint,
        explore_mode=os.environ.get("EXPLORE_MODE", "agent"))
    app.state.deps = deps
    app.state.graph = build_graph(deps) if deps.version == "v2" else None
    app.state.instance_id = id(deps)
    yield
    await mcp.stop()
    deps.session_store.close()
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


BUILD = (Path(__file__).parent / "VERSION").read_text(encoding="utf-8").strip() \
    if (Path(__file__).parent / "VERSION").exists() else "unknown"


@app.get("/health")
async def health():
    d = app.state.deps
    return {"status": "ok", "build": BUILD, "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT, "supervisor_version": d.version,
            "llm_backend": app.state.llm_backend, "fast_llm": d.fast_llm.name,
            "quality_llm": d.quality_llm.name, "cloud_enabled": _POLICY.cloud_enabled,
            "explore_mode": d.explore_mode}


@app.get("/repos", dependencies=[Depends(require_key)])
async def repos():
    out = []
    for name in _POLICY.allowed_repos:
        out.append({"repo": name, "sensitive": name in _POLICY.sensitive_repos})
    return {"repos": out}


@app.get("/flows/{repo}", dependencies=[Depends(require_key)])
async def flows(repo: str, symbol: str, depth: int = 3):
    """LLM 없이 정적 분석 결과만으로 호출 체인을 돌려준다."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("flows", repo=repo, symbol=symbol)
    try:
        res = await app.state.deps.mcp.call_tool(
            "get_chain", {"repo": repo, "symbol": symbol, "depth": depth})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)[:200])
    return res


@app.get("/graph/{repo}", dependencies=[Depends(require_key)])
async def graph_overview(repo: str):
    """구조 개요 — 클래스·모듈 단위 의존 그래프(정적 분석 집계, LLM 없음)."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("graph", repo=repo)
    return await app.state.deps.mcp.call_tool("get_overview", {"repo": repo})


@app.get("/group/{repo}", dependencies=[Depends(require_key)])
async def group_detail(repo: str, id: str):
    """구조 개요에서 묶음을 더블클릭했을 때 — 그 안의 함수들과 호출 (LLM 없음)."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("group", repo=repo, group=id)
    try:
        return await app.state.deps.mcp.call_tool("get_group", {"repo": repo, "group": id})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)[:200])


@app.get("/source/{repo}", dependencies=[Depends(require_key)])
async def source(repo: str, symbol: str):
    """함수 본문 — 답변과 코드를 나란히 보기 위한 조회. MCP get_source를 거쳐 경로 이탈 차단·길이 제한·마스킹이 적용된다.
    회사 서버 안에서 사용자 화면으로만 가며, 감사 로그에는 함수 이름만 남긴다."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("source", repo=repo, symbol=symbol)
    try:
        return await app.state.deps.mcp.call_tool("get_source", {"repo": repo, "symbol": symbol})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)[:200])


@app.get("/file/{repo}", dependencies=[Depends(require_key)])
async def file_view(repo: str, symbol: str = "", path: str = ""):
    """파일 전체 — 함수 하나가 아니라 그 함수가 든 파일 전체를 보여 준다(해당 함수 줄 강조).
    인덱스된 소스 파일만 열 수 있고, 마스킹이 적용된다. 감사 로그에는 파일·함수 이름만 남긴다."""
    require_repo(repo, _POLICY.allowed_repos)
    if not symbol and not path:
        raise HTTPException(status_code=400, detail="symbol or path required")
    audit.record("file", repo=repo, symbol=symbol or None, path=path or None)
    try:
        return await app.state.deps.mcp.call_tool("get_file", {"repo": repo, "symbol": symbol, "path": path})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)[:200])


def _structure_out(s: dict) -> dict:
    return {"entry": s["qualname"], "pairs": s["pairs"], "unresolved": s["unresolved"],
            "scope": s.get("scope", "function"), "entries": s.get("entries") or [s["qualname"]]}


def _suggestions(structure: dict | None, doc_check: dict | None) -> list[dict]:
    """답변 아래 '이어서 물어보기' — 분석 결과에서 결정적으로 만든다(LLM 없음). 실제로 있는 함수만 쓴다.
    ask: 그대로 질문으로 보낸다 / impact: 영향 범위(이 함수를 부르는 곳)를 연다."""
    out: list[dict] = []
    if structure:
        fan: dict[str, int] = {}
        for a, _ in structure["pairs"]:
            fan[a] = fan.get(a, 0) + 1
        entries = structure.get("entries") or [structure["qualname"]]
        if structure.get("scope") == "class":
            for m in sorted(entries, key=lambda m: -fan.get(m, 0))[:2]:
                if fan.get(m):
                    out.append({"label": f"{m} 호출 흐름", "ask": f"{m} 호출 흐름"})
        else:
            subs = [b for a, b in structure["pairs"] if a == entries[0] and fan.get(b)]
            for b in sorted(subs, key=lambda b: -fan[b])[:2]:
                out.append({"label": f"{b} 더 깊게", "ask": f"{b} 호출 흐름"})
            out.append({"label": f"{entries[0]}를 바꾸면 영향받는 곳", "impact": entries[0]})
            owner = entries[0].rsplit(".", 1)[0] if "." in entries[0] else ""
            if owner:
                out.append({"label": f"{owner} 전체 흐름", "ask": f"{owner} 전체 흐름"})
    if doc_check:
        for c in doc_check.get("claims", []):
            if c["verdict"] in ("mismatch", "unknown"):
                out.append({"label": f"{c['caller']} 실제 호출 흐름", "ask": f"{c['caller']} 호출 흐름"})
                break
    seen, uniq = set(), []
    for s in out:
        if s["label"] not in seen:
            seen.add(s["label"]); uniq.append(s)
    return uniq[:4]


def _answer_symbols(structure: dict | None, doc_check: dict | None) -> list[str]:
    """답변과 관련된 함수 목록(코드 보기 버튼용). 코드에 실제로 있는 이름만 넣는다."""
    out: list[str] = []
    if structure:
        out += structure.get("entries") or [structure["qualname"]]   # 클래스 범위면 클래스 이름 대신 메서드들
        for a, b in structure["pairs"]:
            out += [a, b]
    if doc_check:
        rel = doc_check.get("relation")
        if rel:
            out.append(rel["symbol"])
        for c in doc_check.get("claims", []):
            if c["verdict"] != "missing":
                out += [c["caller"], c["callee"]]
            for q in c.get("path") or []:
                out.append(q)
    seen: list[str] = []
    for q in out:
        if q not in seen:
            seen.append(q)
    return seen[:30]


@app.get("/callers/{repo}", dependencies=[Depends(require_key)])
async def callers(repo: str, symbol: str, depth: int = 3):
    """이 함수를 직접·간접으로 부르는 쪽 — 변경 영향 파악."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("callers", repo=repo, symbol=symbol)
    try:
        return await app.state.deps.mcp.call_tool(
            "get_callers", {"repo": repo, "symbol": symbol, "depth": depth})
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)[:200])


@app.get("/audit", dependencies=[Depends(require_key)])
async def audit_tail(n: int = 20):
    try:
        lines = audit.AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        lines = []
    return {"entries": [json.loads(l) for l in lines]}


def _metadata(result: dict, run_id: str | None) -> dict:
    return {
        "route": result.get("route"), "mode": result.get("mode"),
        "decision": result.get("decision"), "sensitive": result.get("sensitive"),
        "tool_calls": result.get("tool_calls", 0),
        "explain_attempts": result.get("attempts", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "llm_backend": app.state.llm_backend,
        "supervisor_version": app.state.deps.version,
        "langsmith_run_id": run_id,
    }


def _error_text(e: Exception) -> str:
    """사용자에게 보여 줄 오류 문장 — 내부 경로·스택은 넣지 않는다."""
    name = type(e).__name__
    if name == "TransientError":
        return "코드 분석 서버가 응답하지 않아 다시 띄웠습니다. 잠시 뒤 다시 질문해 주세요."
    if name == "LLMUnavailable":
        return "모델 서버가 응답하지 않습니다. 잠시 뒤 다시 시도해 주세요."
    return f"요청을 처리하지 못했습니다 ({name}). 같은 문제가 계속되면 관리자에게 알려 주세요."


def _initial_state(req: ChatRequest, history: list[dict]) -> dict:
    return {"query": req.message, "thread_id": req.thread_id, "repo": req.repo,
            "history": history, "request_sensitive": req.sensitive, "attested": req.attested,
            "events": [], "cost_usd": 0.0, "attempts": 0}


_FEEDBACK = FeedbackSender()


def _send_feedback(run_id: str | None, traced: bool, verdict: dict | None, attempts: int,
                   mode: str | None, caught: int) -> dict:
    """추적이 허용된 요청이면 검증 결과를 숫자 피드백으로 붙인다(비동기, 실패해도 응답에 영향 없음).
    감사 로그에는 run_id(식별자)만 남긴다."""
    if not (traced and run_id):
        return {"traced": False}
    scores = scores_from(verdict, attempts, mode, caught)
    try:
        asyncio.get_running_loop().run_in_executor(None, _FEEDBACK.send, run_id, scores)
    except RuntimeError:
        _FEEDBACK.send(run_id, scores)
    return {"traced": True, "run_id": run_id}


def _traced(repo: str, sensitive: bool) -> bool:
    return tracing_allowed(_POLICY, is_sensitive(_POLICY, repo, sensitive)) and has_key() \
        and os.environ.get("LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", "")).lower() == "true"


def _trace_ctx(req_repo: str, req_sensitive: bool):
    allowed = tracing_allowed(_POLICY, is_sensitive(_POLICY, req_repo, req_sensitive))
    return tracing_context(enabled=allowed)


@traceable(name="chat_request")
async def _handle_chat(req: ChatRequest) -> tuple[dict, str | None]:
    deps = app.state.deps
    history = deps.session_store.load(req.thread_id)
    if deps.version == "v2":
        result = await app.state.graph.ainvoke(_initial_state(req, history))
    else:
        result = await run_v1(deps, req.message, req.repo, req.thread_id, history)
    deps.session_store.append(req.thread_id, "user", req.message)
    deps.session_store.append(req.thread_id, "assistant", result["final"])
    rt = get_current_run_tree()
    return result, (str(rt.id) if rt else None)


@app.post("/chat", dependencies=[Depends(require_key)])
async def chat(req: ChatRequest):
    require_repo(req.repo, _POLICY.allowed_repos)
    try:
        with _trace_ctx(req.repo, req.sensitive):
            result, run_id = await _handle_chat(req)
    except Exception as e:     # 트러블슈팅 ④: 원인을 숨긴 500 대신 이유를 알려 주고 감사 로그에 남긴다
        audit.record("chat_error", repo=req.repo, thread_id=req.thread_id, error=type(e).__name__,
                     detail=str(e)[:160], message_chars=len(req.message))
        raise HTTPException(status_code=503, detail=_error_text(e))
    fb = _send_feedback(run_id, _traced(req.repo, req.sensitive), result.get("verdict"),
                        result.get("attempts", 0), result.get("mode"), 0)
    audit.record("chat", repo=req.repo, thread_id=req.thread_id,
                 decision=result.get("decision"), mode=result.get("mode"),
                 attempts=result.get("attempts", 0), message_chars=len(req.message), **fb)
    out = {"reply": result["final"], "events": result.get("events", []),
           "metadata": _metadata(result, run_id)}
    if result.get("structure"):
        out["structure"] = _structure_out(result["structure"])
    if result.get("verdict"):
        out["verification"] = result["verdict"]
    out["symbols"] = _answer_symbols(result.get("structure"), result.get("doc_check"))
    out["suggestions"] = _suggestions(result.get("structure"), result.get("doc_check"))
    return out


@app.get("/chat/stream", dependencies=[Depends(require_key)])
async def chat_stream(message: str, thread_id: str = "default", repo: str = "sample_py",
                      sensitive: bool = False, attested: bool = False):
    """그래프의 진행 상황을 SSE로 스트리밍. 이벤트마다 'data: {json}\\n\\n', 마지막에 data: [DONE]."""
    require_repo(repo, _POLICY.allowed_repos)
    deps = app.state.deps
    req = ChatRequest(message=message, thread_id=thread_id, repo=repo,
                      sensitive=sensitive, attested=attested)

    async def gen():
        history = deps.session_store.load(thread_id)
        if deps.version != "v2":
            result = await run_v1(deps, message, repo, thread_id, history)
            for ev in result["events"]:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': result['final']}, ensure_ascii=False)}\n\n"
            final = result["final"]
            verdict = structure = None
            info = {"mode": result.get("mode")}
        else:
            seen, final, verdict, structure, doc_check = 0, "", None, None, None
            info: dict = {"caught": 0}
            run_id = str(uuid.uuid4())     # 그래프 루트 실행 id — LangSmith 피드백·관리자 링크에 쓴다
            try:
                with _trace_ctx(repo, sensitive):
                    async for mode, payload in app.state.graph.astream(
                            _initial_state(req, history), stream_mode=["updates", "custom"],
                            config={"run_id": run_id, "run_name": "re-cat.chat"}):
                        if mode == "custom":
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            continue
                        for node_name, delta in payload.items():
                            for ev in (delta.get("events") or [])[seen:]:
                                if ev.get("type") == "Verification":   # 시도마다 반려된 오류를 합산한다
                                    info["caught"] = info.get("caught", 0) + ev.get("hallucinated", 0) + ev.get("wrong", 0)
                                yield f"data: {json.dumps({**ev, 'node': node_name}, ensure_ascii=False)}\n\n"
                            if delta.get("events"):
                                seen = len(delta["events"])
                            final = delta.get("final") or final
                            verdict = delta.get("verdict") or verdict
                            for k in ("mode", "attempts", "decision", "sensitive", "tool_calls"):
                                if delta.get(k) is not None:
                                    info[k] = delta[k]
                            structure = delta.get("structure") or structure
                            doc_check = delta.get("doc_check") or doc_check
            except Exception as e:
                # 트러블슈팅 ④: 예전에는 여기서 연결이 그냥 끊겨 화면엔 '연결이 끊겼습니다'만, 감사 로그엔 아무것도 없었다
                audit.record("chat_error", repo=repo, thread_id=thread_id, error=type(e).__name__,
                             detail=str(e)[:160], message_chars=len(message), stream=True)
                yield f"data: {json.dumps({'type': 'Error', 'message': _error_text(e)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': final, 'verification': verdict, 'structure': _structure_out(structure) if structure else None, 'symbols': _answer_symbols(structure, doc_check), 'suggestions': _suggestions(structure, doc_check)}, ensure_ascii=False)}\n\n"
        deps.session_store.append(thread_id, "user", message)
        deps.session_store.append(thread_id, "assistant", final)
        v = verdict or {}
        fb = _send_feedback(run_id if deps.version == "v2" else None, _traced(repo, sensitive), verdict,
                            info.get("attempts", 0), info.get("mode"), info.get("caught", 0))
        audit.record("chat_stream", repo=repo, thread_id=thread_id, message_chars=len(message),
                     mode=info.get("mode"), attempts=info.get("attempts", 0),
                     decision=info.get("decision"), sensitive=info.get("sensitive"),
                     tool_calls=info.get("tool_calls", 0), caught=info.get("caught", 0),
                     verified=v.get("verified", 0), hallucinated=v.get("hallucinated", 0),
                     wrong=v.get("wrong", 0), unverifiable=v.get("unverifiable", 0), **fb)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _read_audit() -> list[dict]:
    try:
        lines = audit.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    return out


def _audit_stats(entries: list[dict]) -> dict:
    chats = [e for e in entries if e["event"] in ("chat", "chat_stream")]
    by = lambda key: {k: sum(1 for e in chats if e.get(key) == k)
                      for k in sorted({e.get(key) for e in chats if e.get(key) is not None}, key=str)}
    v = {k: sum(e.get(k, 0) or 0 for e in chats) for k in ("verified", "hallucinated", "wrong", "unverifiable")}
    claims = sum(v.values())
    return {"total_requests": len(entries), "chats": len(chats),
            "by_event": {k: sum(1 for e in entries if e["event"] == k)
                         for k in sorted({e["event"] for e in entries})},
            "by_repo": by("repo"), "by_decision": by("decision"), "by_mode": by("mode"),
            "retried": sum(1 for e in chats if (e.get("attempts") or 0) > 1),
            "fallbacks": sum(1 for e in chats if e.get("mode") == "no-llm-fallback"),
            "claims": v, "total_claims": claims,
            # 재설명 전에 걸러낸 오류까지 포함한 값 (최종 응답의 오류는 검증을 통과한 뒤라 항상 0에 가깝다)
            "caught_errors": sum(e.get("caught", 0) or 0 for e in chats)}


def _ls_uploads() -> list[dict]:
    p = Path(__file__).resolve().parent / "eval" / "results" / "langsmith_uploads.jsonl"
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()][-5:][::-1]
    except (OSError, json.JSONDecodeError):
        return []


def _eval_results() -> list[dict]:
    d = Path(__file__).resolve().parent / "eval" / "results"
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:5]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"file": p.name, "backend": r.get("backend"), "when": r.get("when"),
                    "models": r.get("models"), "questions": r.get("questions"),
                    "entry_hit_rate": r.get("entry_hit_rate"), "fallback_rate": r.get("fallback_rate"),
                    "avg_attempts": r.get("avg_attempts"), "summary": r.get("summary"),
                    "demo_only": r.get("backend") == "fake"})
    return out


@app.get("/admin/summary", dependencies=[Depends(require_admin)])
async def admin_summary():
    """관리자 화면용 — 상태, 정책, 감사 통계, 실험 결과. 코드 본문은 어디에도 없다."""
    d = app.state.deps
    entries = _read_audit()
    return {
        "health": {"llm_backend": app.state.llm_backend, "fast_llm": d.fast_llm.name,
                   "quality_llm": d.quality_llm.name, "explore_mode": d.explore_mode,
                   "supervisor_version": d.version, "creation_count": CREATION_COUNT},
        "policy": {"cloud_enabled": _POLICY.cloud_enabled, "allowed_endpoints": _POLICY.allowed_endpoints,
                   "require_attestation": _POLICY.require_attestation,
                   "require_masking": _POLICY.require_masking,
                   "max_snippet_chars": _POLICY.max_snippet_chars,
                   "max_tool_calls": _POLICY.max_tool_calls, "max_depth": _POLICY.max_depth,
                   "max_explain_retries": _POLICY.max_explain_retries,
                   "repos": [{"repo": r, "sensitive": r in _POLICY.sensitive_repos}
                             for r in _POLICY.allowed_repos]},
        "tracing": {"langsmith_enabled_by_policy": _POLICY.langsmith_enabled,
                    "hide_inputs_outputs": _POLICY.hide_inputs_outputs,
                    "endpoint": tracing_endpoint(),
                    "endpoint_internal": tracing_endpoint() in {e.rstrip("/") for e in _POLICY.langsmith_internal_endpoints},
                    "decision_sensitive": tracing_decision(_POLICY, True)[1],
                    "decision_normal": tracing_decision(_POLICY, False)[1],
                    "env_tracing": os.environ.get("LANGSMITH_TRACING", "unset"),
                    "api_key_present": has_key(),
                    "feedback": {"sent": _FEEDBACK.sent, "failed": _FEEDBACK.failed},
                    "run_url_template": bool(os.environ.get("LANGSMITH_RUN_URL")),
                    "evaluation_integration": True,
                    "uploads": _ls_uploads()},
        "stats": _audit_stats(entries),
        "recent": [{**e, "run_url": run_url(e.get("run_id"))} for e in entries[-30:][::-1]],
        "eval_results": _eval_results(),
    }


class RegisterRepo(BaseModel):
    repo: str
    sensitive: bool = True        # 기본은 민감(클라우드·LangSmith 차단) — fail-closed


async def _repo_row(repo: str) -> dict:
    st = await app.state.deps.mcp.call_tool("get_status", {"repo": repo})
    reg = registry.registered().get(repo)
    return {"repo": repo, "sensitive": repo in _POLICY.sensitive_repos,
            "source": (reg.get("source") == "upload" and "upload" or "registered") if reg else "policy", **st}


_ADMIN_WRITE = asyncio.Lock()


async def _admin_write():
    """관리자 쓰기 작업(등록·업로드·문서·재분석)을 한 번에 하나씩 — 트러블슈팅 ⑦.
    '이미 있나?' 확인과 설치·등록 사이에 다른 요청이 끼어들어, 실패 응답을 받은 요청의 파일이
    성공한 요청의 파일을 덮어쓰던 문제를 막는다. (서버 프로세스 1개 기준. 여러 워커로 띄우면 파일 잠금이 필요)"""
    async with _ADMIN_WRITE:
        yield


@app.get("/admin/repos", dependencies=[Depends(require_admin)])
async def admin_repos():
    """등록된 저장소의 분석 상태와 등록 후보(data/repos/ 바로 아래 폴더). 코드 본문은 없다."""
    registry.apply(_POLICY)
    rows = [await _repo_row(r) for r in _POLICY.allowed_repos]
    return {"repos": rows, "candidates": registry.candidates(_POLICY)}


@app.post("/admin/repos", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_register_repo(req: RegisterRepo):
    try:
        registry.register(_POLICY, req.repo, req.sensitive)
    except registry.RegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record("repo_register", repo=req.repo, sensitive=req.sensitive)
    return await _repo_row(req.repo)


def _flag(v: str, default: bool) -> bool:
    v = (v or "").strip().lower()
    if default:                       # 기본 true인 값(민감)은 명시적으로 false라고 할 때만 끈다 (fail-closed)
        return v not in ("false", "0", "off", "no")
    return v in ("true", "1", "on", "yes")


async def _install_zip(tmp_path: Path, repo: str, sensitive: bool, replace: bool, kind: str) -> dict:
    """zip(또는 폴더를 묶은 zip)을 저장소로 설치하고 등록·재분석한다."""
    from security.path_guard import REPOS_ROOT
    from security.repo_upload import UploadRejected, extract_repo
    registry.apply(_POLICY)
    reg = registry.registered().get(repo)
    if replace:
        if repo not in _POLICY.allowed_repos or not reg or reg.get("source") != "upload":
            raise HTTPException(status_code=400, detail="교체는 업로드로 등록한 저장소만 가능합니다")
    elif repo in _POLICY.allowed_repos:
        raise HTTPException(status_code=400, detail="이미 등록된 저장소입니다 (교체하려면 replace)")
    try:
        stats = await asyncio.to_thread(extract_repo, tmp_path, repo, REPOS_ROOT, replace=replace)
    except UploadRejected as e:
        audit.record("repo_upload_rejected", repo=repo, kind=kind, reason=str(e)[:120])
        raise HTTPException(status_code=400, detail=str(e))
    if replace:
        await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})
    else:
        try:
            registry.register(_POLICY, repo, sensitive, source="upload")
        except registry.RegistryError as e:
            raise HTTPException(status_code=400, detail=str(e))
    audit.record("repo_upload", repo=repo, kind=kind, files=stats["files"], bytes=stats["bytes"],
                 sensitive=repo in _POLICY.sensitive_repos, replace=replace)
    return {**(await _repo_row(repo)), "uploaded_files": stats["files"]}


@app.post("/admin/repos/upload", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_repo(file: UploadFile = File(...), repo: str = Form(...),
                            sensitive: str = Form("true"), replace: str = Form("false")):
    """zip으로 저장소를 올린다. 새 이름이면 등록(기본 민감), replace=true면 업로드로 등록했던 저장소만 교체 후 재분석."""
    import shutil
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        return await _install_zip(tmp_path, repo, _flag(sensitive, True), _flag(replace, False), "zip")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/admin/repos/upload-folder", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_folder(files: list[UploadFile] = File(...), repo: str = Form(...),
                              sensitive: str = Form("true"), replace: str = Form("false")):
    """브라우저에서 폴더를 통째로 고른 경우. 파일 이름(상대 경로)을 그대로 zip으로 묶은 뒤 zip과 같은 검사를 거친다."""
    import tempfile
    import zipfile
    from security.repo_upload import Limits
    lim = Limits()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    total = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if len(files) > lim.max_files * 2:
                raise HTTPException(status_code=400, detail="파일이 너무 많습니다")
            for f in files:
                data = await f.read()
                total += len(data)
                if total > lim.max_total_bytes:
                    raise HTTPException(status_code=400, detail="폴더 크기가 상한을 넘습니다")
                zf.writestr(f.filename or "unnamed", data)      # 경로 검사는 extract_repo가 한다
        return await _install_zip(tmp_path, repo, _flag(sensitive, True), _flag(replace, False), "folder")
    finally:
        tmp_path.unlink(missing_ok=True)


DOC_EXT = {".md", ".markdown", ".txt", ".rst"}


@app.get("/admin/repos/{repo}/docs", dependencies=[Depends(require_admin)])
async def admin_list_docs(repo: str):
    from security.path_guard import docs_dir
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    files = sorted(({"name": p.name, "bytes": p.stat().st_size} for p in d.iterdir() if p.is_file() and not p.is_symlink()),
                   key=lambda x: x["name"]) if d.is_dir() else []
    return {"repo": repo, "uploaded": files}


@app.post("/admin/repos/{repo}/docs", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_docs(repo: str, files: list[UploadFile] = File(...)):
    """저장소에 문서(기획서·명세·README 등 md·txt·rst)를 따로 올린다. 저장소 코드 폴더는 건드리지 않고
    data/docs/<repo>/ 에 저장하며, 문서 검색·문서-코드 대조에 바로 반영된다."""
    from security.path_guard import docs_dir
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    saved, total = [], 0
    for f in files:
        name = Path((f.filename or "").replace("\\", "/")).name
        if not name or name.startswith(".") or len(name) > 120 or "\x00" in name or Path(name).suffix.lower() not in DOC_EXT:
            raise HTTPException(status_code=400, detail=f"문서는 md·txt·rst만 올릴 수 있습니다: {f.filename}")
        data = await f.read()
        total += len(data)
        if len(data) > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="문서 크기 상한(파일 2MB, 합계 20MB)을 넘습니다")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=f"UTF-8 텍스트가 아닙니다: {name}")
        saved.append((name, data))
    d.mkdir(parents=True, exist_ok=True)
    for name, data in saved:
        (d / name).write_bytes(data)
    await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})     # 문서 색인 갱신
    audit.record("docs_upload", repo=repo, files=len(saved), bytes=total)
    return {**(await _repo_row(repo)), "uploaded_docs": [n for n, _ in saved]}


@app.delete("/admin/repos/{repo}/docs/{name}", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_delete_doc(repo: str, name: str):
    """업로드한 문서만 지운다(저장소 안 문서는 지울 수 없다)."""
    from security.path_guard import docs_dir, safe_file
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    try:
        p = safe_file(d, name) if d.is_dir() else None
    except ValueError:
        p = None
    if p is None or p == d or p.parent != d or not p.is_file():
        raise HTTPException(status_code=404, detail="문서가 없습니다")
    p.unlink()
    await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})
    audit.record("docs_delete", repo=repo, name=name)
    return await _repo_row(repo)


@app.post("/admin/repos/{repo}/reanalyze", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_reanalyze(repo: str):
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    res = await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})
    audit.record("repo_reanalyze", repo=repo,
                 functions_before=(res.get("before") or {}).get("function_count"),
                 functions_after=res["after"]["function_count"])
    return res


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
