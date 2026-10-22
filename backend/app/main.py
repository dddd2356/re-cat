"""Re-cat FastAPI 애플리케이션 진입점.

정적 분석·MCP·Ollama·PostgreSQL을 조합하고, API 오류는 ``core.errors``의 공통 계약으로 응답한다.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]

import logging
import time
import uuid

from backend.app.observability.langsmith_feedback import FeedbackSender, has_key, run_url, scores_from
from backend.app.security import registry
from backend.app.security.policy import load_policy

def _apply_tracing_environment(policy) -> None:
    """정책보다 기존 배포 환경변수가 우선하지 않도록 추적 환경을 닫힌 쪽으로 맞춘다."""
    # 입출력 숨김은 LangSmith 클라이언트가 만들어지기 전에 설정한다.
    # setdefault를 쓰면 배포 환경에 남아 있던 false 값이 정책을 우회할 수 있다.
    if policy.hide_inputs_outputs:
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
    if not policy.langsmith_enabled:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"


_POLICY = load_policy()   # policy.yaml + 관리자가 등록한 저장소(더하기만)
_apply_tracing_environment(_POLICY)

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from langsmith import traceable, tracing_context
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, Field

from backend.app.agents.deps import SupervisorDeps
from backend.app.agents.graph import build_graph
from backend.app.agents.mcp_client import MCPClient
from backend.app.agents.session_store import PostgresSessionStore
from backend.app.agents.supervisor_v1 import run_v1
from backend.app.security import audit
from backend.app.security.auth import require_admin, require_key, require_repo
from backend.app.security.gate import is_sensitive, tracing_allowed, tracing_decision, tracing_endpoint
from backend.app.core.errors import AppError, ErrorCode, code_for_status, error_body

LOG = logging.getLogger("recat.api")

CREATION_COUNT = 0   # 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


def _validate_runtime_config() -> None:
    """배포 모드에서 인증 없는 기동을 막는다.

    개발/테스트의 로컬 편의성은 유지하되, 운영자가 명시적으로
    ``RECAT_ENV=production``을 선택한 경우에는 빈 키나 같은 역할 키로
    서버가 열리지 않게 한다. SSO 전환 전 단일 조직 PoC의 최소 경계다.
    """
    env = os.environ.get("RECAT_ENV", "development").strip().lower()
    if env not in {"development", "test", "production"}:
        raise RuntimeError("RECAT_ENV must be development, test, or production")
    if env != "production":
        return
    user_key = os.environ.get("RECAT_API_KEY", "").strip()
    admin_key = os.environ.get("RECAT_ADMIN_KEY", "").strip()
    if not user_key or not admin_key:
        raise RuntimeError("RECAT_API_KEY and RECAT_ADMIN_KEY are required in production")
    if user_key == admin_key:
        raise RuntimeError("RECAT_API_KEY and RECAT_ADMIN_KEY must differ in production")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str = "default"
    repo: str = "sample_py"
    sensitive: bool = False     # 요청 단위로 민감 처리를 강제 (저장소 정책과 OR)
    attested: bool = False      # 고객이 외부 전송을 명시적으로 확인했는가


def _build_llms():
    """LLM_BACKEND=ollama만 허용한다. CLOUD_BACKEND=openai면 클라우드 설명 모델도 만든다."""
    backend = os.environ.get("LLM_BACKEND", "ollama")
    if backend == "ollama":
        from backend.app.agents.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"))
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"))
    else:
        raise ValueError("LLM_BACKEND must be ollama")
    cloud, endpoint = None, None
    if os.environ.get("CLOUD_BACKEND") == "openai":
        from backend.app.agents.openai_llm import OpenAIQualityLLM
        cloud = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"), 0.005)
        endpoint = cloud.endpoint()
    return backend, fast, quality, cloud, endpoint


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    from backend.app.repositories import db
    from backend.app.repositories.deletion import recover
    _validate_runtime_config()
    db.check()
    recover()
    registry.apply(_POLICY)
    CREATION_COUNT += 1
    mcp = MCPClient(flaky_first_tool_call=os.environ.get("RECAT_FLAKY") == "1")
    await mcp.start()    # MCP subprocess는 서버 수명 동안 딱 1개
    backend, fast_llm, quality_llm, cloud_llm, cloud_endpoint = _build_llms()
    app.state.llm_backend = backend
    deps = SupervisorDeps(
        fast_llm=fast_llm, quality_llm=quality_llm, mcp=mcp,
        session_store=PostgresSessionStore(),
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


app = FastAPI(
    title="Re-cat API",
    summary="온프레미스 레거시 코드 호출 흐름·문서 근거 분석 API",
    description=("정적 분석으로 호출 관계를 확인하고, Ollama가 설명을 작성합니다. "
                 "관리자 API는 `RECAT_ADMIN_KEY` Bearer 인증이 필요합니다."),
    version="2026-10-21.3",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """응답·로그 상관관계용 ID만 남긴다. 요청 본문이나 인증 값은 기록하지 않는다."""
    request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    LOG.info("request_completed request_id=%s method=%s path=%s status=%s duration_ms=%d",
             request.state.request_id, request.method, request.url.path, response.status_code,
             (time.perf_counter() - started) * 1000)
    return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    LOG.warning("request_failed request_id=%s code=%s status=%s path=%s",
                _request_id(request), exc.code, exc.status_code, request.url.path)
    return JSONResponse(error_body(exc.code, exc.message, _request_id(request)), status_code=exc.status_code)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    # 이전 엔드포인트의 HTTPException도 같은 계약으로 내보내며 단계적으로 AppError로 옮긴다.
    message = exc.detail if isinstance(exc.detail, str) else "요청을 처리할 수 없습니다."
    return JSONResponse(error_body(code_for_status(exc.status_code), message, _request_id(request)),
                        status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    LOG.info("request_invalid request_id=%s path=%s errors=%d",
             _request_id(request), request.url.path, len(exc.errors()))
    return JSONResponse(error_body(ErrorCode.VALIDATION_FAILED, "요청 형식이 올바르지 않습니다.", _request_id(request)),
                        status_code=422)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    """예상하지 못한 오류도 공통 계약으로 응답하되, 예외 내용은 외부로 노출하지 않는다."""
    LOG.error("request_failed request_id=%s code=%s status=500 path=%s",
              _request_id(request), ErrorCode.INTERNAL_ERROR, request.url.path)
    return JSONResponse(error_body(ErrorCode.INTERNAL_ERROR, "요청을 처리할 수 없습니다.", _request_id(request)),
                        status_code=500, headers={"X-Request-ID": _request_id(request)})


BUILD = (ROOT / "VERSION").read_text(encoding="utf-8").strip() \
    if (ROOT / "VERSION").exists() else "unknown"


@app.get("/health")
async def health():
    d = app.state.deps
    return {"status": "ok", "build": BUILD, "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT, "supervisor_version": d.version,
            "llm_backend": app.state.llm_backend, "fast_llm": d.fast_llm.name,
            "quality_llm": d.quality_llm.name, "cloud_enabled": _POLICY.cloud_enabled,
            "explore_mode": d.explore_mode}


async def _repo_read():
    # 삭제·업로드와 조회를 직렬화해 삭제 도중 캐시가 다시 생기지 않게 한다.
    async with _ADMIN_WRITE:
        registry.apply(_POLICY)
        yield


@app.get("/repos", dependencies=[Depends(require_key), Depends(_repo_read)])
async def repos():
    out = []
    for name in _POLICY.allowed_repos:
        out.append({"repo": name, "sensitive": name in _POLICY.sensitive_repos})
    return {"repos": out}


@app.get("/flows/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def flows(repo: str, symbol: str, depth: int = 3):
    """LLM 없이 정적 분석 결과만으로 호출 체인을 돌려준다."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("flows", repo=repo, symbol=symbol)
    try:
        res = await app.state.deps.mcp.call_tool(
            "get_chain", {"repo": repo, "symbol": symbol, "depth": depth})
    except Exception:
        raise AppError(ErrorCode.REPO_NOT_FOUND, 404, "호출 흐름을 찾을 수 없습니다.") from None
    return res


@app.get("/graph/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def graph_overview(repo: str):
    """구조 개요 — 클래스·모듈 단위 의존 그래프(정적 분석 집계, LLM 없음)."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("graph", repo=repo)
    return await app.state.deps.mcp.call_tool("get_overview", {"repo": repo})


@app.get("/group/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def group_detail(repo: str, id: str):
    """구조 개요에서 묶음을 더블클릭했을 때 — 그 안의 함수들과 호출 (LLM 없음)."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("group", repo=repo, group=id)
    try:
        return await app.state.deps.mcp.call_tool("get_group", {"repo": repo, "group": id})
    except Exception:
        raise AppError(ErrorCode.REPO_NOT_FOUND, 404, "그룹을 찾을 수 없습니다.") from None


@app.get("/source/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def source(repo: str, symbol: str):
    """함수 본문 — 답변과 코드를 나란히 보기 위한 조회. MCP get_source를 거쳐 경로 이탈 차단·길이 제한·마스킹이 적용된다.
    회사 서버 안에서 사용자 화면으로만 가며, 감사 로그에는 함수 이름만 남긴다."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("source", repo=repo, symbol=symbol)
    try:
        return await app.state.deps.mcp.call_tool("get_source", {"repo": repo, "symbol": symbol})
    except Exception:
        raise AppError(ErrorCode.REPO_NOT_FOUND, 404, "함수 소스를 찾을 수 없습니다.") from None


@app.get("/file/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def file_view(repo: str, symbol: str = "", path: str = ""):
    """파일 전체 — 함수 하나가 아니라 그 함수가 든 파일 전체를 보여 준다(해당 함수 줄 강조).
    인덱스된 소스 파일만 열 수 있고, 마스킹이 적용된다. 감사 로그에는 파일·함수 이름만 남긴다."""
    require_repo(repo, _POLICY.allowed_repos)
    if not symbol and not path:
        raise AppError(ErrorCode.REQUEST_INVALID, 400, "symbol 또는 path가 필요합니다.")
    audit.record("file", repo=repo, symbol=symbol or None, path=path or None)
    try:
        return await app.state.deps.mcp.call_tool("get_file", {"repo": repo, "symbol": symbol, "path": path})
    except Exception:
        raise AppError(ErrorCode.REPO_NOT_FOUND, 404, "파일을 찾을 수 없습니다.") from None


def _structure_out(s: dict) -> dict:
    return {"entry": s["qualname"], "pairs": s["pairs"], "unresolved": s["unresolved"],
            "scope": s.get("scope", "function"), "entries": s.get("entries") or [s["qualname"]],
            "entry_ids": s.get("entry_ids", []), "pair_ids": s.get("pair_ids", []),
            "symbol_refs": s.get("symbol_refs", [])}


def _suggestions(structure: dict | None, doc_check: dict | None, clarify: list[str] | None = None) -> list[dict]:
    """답변 아래 '이어서 물어보기' — 분석 결과에서 결정적으로 만든다(LLM 없음). 실제로 있는 함수만 쓴다.
    ask: 그대로 질문으로 보낸다 / impact: 영향 범위(이 함수를 부르는 곳)를 연다.
    clarify: 되묻기 후보(진입점 검색이 애매할 때, locate_node가 고른 상위 1~3개) — 다른 제안과 섞지 않는다."""
    if clarify:
        return [{"label": qn, "ask": f"{qn} 호출 흐름"} for qn in clarify]
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


def _answer_symbols(structure: dict | None, doc_check: dict | None) -> list[dict]:
    """코드 보기 버튼용 고유 ID와 표시 이름. 이름이 중복돼도 다른 파일을 열지 않는다."""
    out: list[dict] = []
    if structure:
        refs = {ref["id"]: ref for ref in structure.get("symbol_refs", [])}
        ids = list(structure.get("entry_ids", []))
        for a, b in structure.get("pair_ids", []):
            ids += [a, b]
        for sid in ids:
            if sid in refs and refs[sid] not in out:
                out.append(refs[sid])
    if doc_check:
        for ref in doc_check.get("code_evidence_refs", []):
            if ref not in out:
                out.append(ref)
    return out[:30]


@app.get("/callers/{repo}", dependencies=[Depends(require_key), Depends(_repo_read)])
async def callers(repo: str, symbol: str, depth: int = 3):
    """이 함수를 직접·간접으로 부르는 쪽 — 변경 영향 파악."""
    require_repo(repo, _POLICY.allowed_repos)
    audit.record("callers", repo=repo, symbol=symbol)
    try:
        return await app.state.deps.mcp.call_tool(
            "get_callers", {"repo": repo, "symbol": symbol, "depth": depth})
    except Exception:
        raise AppError(ErrorCode.REPO_NOT_FOUND, 404, "호출자를 찾을 수 없습니다.") from None


@app.get("/audit", dependencies=[Depends(require_admin)])
async def audit_tail(n: int = 20):
    return {"entries": audit.read(n)}


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
    async with _ADMIN_WRITE:
        require_repo(req.repo, _POLICY.allowed_repos)
        return await _handle_chat_locked(req)


async def _handle_chat_locked(req: ChatRequest) -> tuple[dict, str | None]:
    deps = app.state.deps
    history = deps.session_store.load(req.thread_id, req.repo)
    if deps.version == "v2":
        result = await app.state.graph.ainvoke(_initial_state(req, history))
    else:
        result = await run_v1(deps, req.message, req.repo, req.thread_id, history)
    deps.session_store.append_turn(req.thread_id, req.message, result["final"], req.repo)
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
        raise AppError(ErrorCode.LLM_UNAVAILABLE, 503, _error_text(e)) from None
    fb = _send_feedback(run_id, _traced(req.repo, req.sensitive), result.get("verdict"),
                        result.get("attempts", 0), result.get("mode"), 0)
    audit.record("chat", repo=req.repo, thread_id=req.thread_id,
                 decision=result.get("decision"), mode=result.get("mode"),
                 attempts=result.get("attempts", 0), message_chars=len(req.message), **fb)
    out = {"reply": result["final"], "events": result.get("events", []),
           "metadata": _metadata(result, run_id)}
    if result.get("structure"):
        out["structure"] = _structure_out(result["structure"])
    if result.get("verdict") and not result.get("doc_all"):
        out["verification"] = result["verdict"]
    out["symbols"] = [] if result.get("doc_all") else _answer_symbols(result.get("structure"), result.get("doc_check"))
    clarify = ([c["qualname"] for c in result["candidates"]] if result.get("mode") == "clarify" else None)
    out["suggestions"] = [] if result.get("doc_all") else _suggestions(result.get("structure"), result.get("doc_check"), clarify)
    return out


@app.get("/chat/stream", dependencies=[Depends(require_key)])
async def chat_stream(request: Request, message: str, thread_id: str = "default", repo: str = "sample_py",
                      sensitive: bool = False, attested: bool = False):
    """그래프의 진행 상황을 SSE로 스트리밍. 이벤트마다 'data: {json}\\n\\n', 마지막에 data: [DONE]."""
    require_repo(repo, _POLICY.allowed_repos)
    deps = app.state.deps
    req = ChatRequest(message=message, thread_id=thread_id, repo=repo,
                      sensitive=sensitive, attested=attested)

    async def gen():
        history = deps.session_store.load(thread_id, repo)
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
                            for k in ("mode", "attempts", "decision", "sensitive", "tool_calls", "candidates", "doc_all"):
                                if delta.get(k) is not None:
                                    info[k] = delta[k]
                            structure = delta.get("structure") or structure
                            doc_check = delta.get("doc_check") or doc_check
            except Exception as e:
                # 트러블슈팅 ④: 예전에는 여기서 연결이 그냥 끊겨 화면엔 '연결이 끊겼습니다'만, 감사 로그엔 아무것도 없었다
                audit.record("chat_error", repo=repo, thread_id=thread_id, error=type(e).__name__,
                             detail=str(e)[:160], message_chars=len(message), stream=True)
                yield f"data: {json.dumps({'type': 'Error', 'error': error_body(ErrorCode.LLM_UNAVAILABLE, _error_text(e), _request_id(request))['error']}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            clarify = ([c["qualname"] for c in info["candidates"]] if info.get("mode") == "clarify" and info.get("candidates") else None)
            all_doc = bool(info.get("doc_all"))
            yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': final, 'mode': info.get('mode'), 'verification': None if all_doc else verdict, 'structure': _structure_out(structure) if structure else None, 'symbols': [] if all_doc else _answer_symbols(structure, doc_check), 'suggestions': [] if all_doc else _suggestions(structure, doc_check, clarify)}, ensure_ascii=False)}\n\n"
        deps.session_store.append_turn(thread_id, message, final, repo)
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

    async def locked_gen():
        async with _ADMIN_WRITE:
            require_repo(repo, _POLICY.allowed_repos)
            async for event in gen():
                yield event

    return StreamingResponse(locked_gen(), media_type="text/event-stream")


def _read_audit() -> list[dict]:
    return audit.read()


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
    p = ROOT / "tools" / "eval" / "results" / "langsmith_uploads.jsonl"
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()][-5:][::-1]
    except (OSError, json.JSONDecodeError):
        return []


def _eval_results() -> list[dict]:
    d = ROOT / "tools" / "eval" / "results"
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


@app.get("/admin/repos", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_repos():
    """등록된 저장소의 분석 상태와 등록 후보(var/data/repos/ 바로 아래 폴더). 코드 본문은 없다."""
    registry.apply(_POLICY)
    rows = [await _repo_row(r) for r in _POLICY.allowed_repos]
    return {"repos": rows, "candidates": registry.candidates(_POLICY)}


@app.post("/admin/repos", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_register_repo(req: RegisterRepo):
    try:
        registry.register(_POLICY, req.repo, req.sensitive)
    except registry.RegistryError:
        raise AppError(ErrorCode.REQUEST_INVALID, 400, "저장소를 등록할 수 없습니다.") from None
    audit.record("repo_register", repo=req.repo, sensitive=req.sensitive)
    return await _repo_row(req.repo)


def _flag(v: str, default: bool) -> bool:
    v = (v or "").strip().lower()
    if default:                       # 기본 true인 값(민감)은 명시적으로 false라고 할 때만 끈다 (fail-closed)
        return v not in ("false", "0", "off", "no")
    return v in ("true", "1", "on", "yes")


def _upload_rejection_message(exc: Exception) -> str:
    """관리자에게는 조치 가능한 거부 범주만 알리고, zip 내부 경로·파일명은 노출하지 않는다."""
    detail = str(exc)
    if "zip이 너무 큽니다" in detail:
        return "압축 파일 크기가 업로드 한도를 넘었습니다."
    if "압축을 푼 크기" in detail:
        return "압축 해제 후 전체 크기가 업로드 한도를 넘었습니다."
    if "파일이 너무 많" in detail:
        return "압축 안의 파일 수가 업로드 한도를 넘었습니다."
    if "파일이 너무 큽" in detail:
        return "압축 안에 파일별 크기 한도를 넘는 항목이 있습니다."
    if "압축률" in detail:
        return "압축률이 비정상적으로 높아 압축 폭탄 가능성으로 거부됐습니다."
    if "링크·특수 파일" in detail:
        return "심볼릭 링크 또는 특수 파일이 포함되어 업로드할 수 없습니다."
    if "허용되지 않는 경로" in detail:
        return "상대 경로 이탈 또는 절대 경로가 포함되어 업로드할 수 없습니다."
    if "zip 파일이 아닙니다" in detail:
        return "선택한 파일이 올바른 zip 형식이 아닙니다."
    if "올릴 파일이 없습니다" in detail:
        return "분석할 수 있는 파일이 압축 안에 없습니다."
    return "압축 파일 보안 검사를 통과하지 못했습니다."


async def _install_zip(tmp_path: Path, repo: str, sensitive: bool, replace: bool, kind: str) -> dict:
    """zip(또는 폴더를 묶은 zip)을 저장소로 설치하고 등록·재분석한다."""
    from backend.app.security.path_guard import REPOS_ROOT
    from backend.app.security.repo_upload import UploadRejected, extract_repo
    registry.apply(_POLICY)
    reg = registry.registered().get(repo)
    if replace:
        if repo not in _POLICY.allowed_repos or not reg or reg.get("source") != "upload":
            raise AppError(ErrorCode.REQUEST_INVALID, 400, "교체는 업로드로 등록한 저장소만 가능합니다.")
    elif repo in _POLICY.allowed_repos:
        raise AppError(ErrorCode.REQUEST_INVALID, 400, "이미 등록된 저장소입니다. 교체하려면 replace를 사용하세요.")
    try:
        stats = await asyncio.to_thread(extract_repo, tmp_path, repo, REPOS_ROOT, replace=replace, keep_backup=True)
    except UploadRejected as e:
        audit.record("repo_upload_rejected", repo=repo, kind=kind, reason=str(e)[:120])
        raise AppError(ErrorCode.UPLOAD_REJECTED, 400, _upload_rejection_message(e)) from None
    import shutil
    try:
        if replace:
            registry.bump(repo)
        else:
            registry.register(_POLICY, repo, sensitive, source="upload")
    except Exception:
        shutil.rmtree(REPOS_ROOT / repo)
        if stats.get("backup"):
            Path(stats["backup"]).rename(REPOS_ROOT / repo)
        raise
    if replace:
        # 새 파일을 확정하기 전에 MCP가 실제로 새 저장소를 다시 읽을 수 있는지 확인한다.
        # 실패하면 extract_repo가 보관한 이전 폴더로 되돌려 파일·등록부·캐시가 서로 다른
        # revision을 보지 않게 한다.
        try:
            await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})
        except Exception:
            backup = Path(stats["backup"]) if stats.get("backup") else None
            if backup and backup.exists():
                shutil.rmtree(REPOS_ROOT / repo, ignore_errors=True)
                backup.rename(REPOS_ROOT / repo)
                registry.bump(repo)     # 새 파일/기존 파일 양쪽 캐시를 무효화한다.
                try:
                    await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})
                except Exception:
                    pass                # 원래 예외를 보존한다. 다음 읽기에서 MCP가 다시 분석한다.
            raise
    if stats.get("backup"):
        shutil.rmtree(stats["backup"])
    audit.record("repo_upload", repo=repo, kind=kind, files=stats["files"], bytes=stats["bytes"],
                 sensitive=repo in _POLICY.sensitive_repos, replace=replace)
    return {**(await _repo_row(repo)), "uploaded_files": stats["files"]}


async def _copy_upload_limited(upload: UploadFile, destination: Path, maximum: int) -> int:
    """UploadFile을 일정 크기씩 복사해 메모리·디스크 고갈을 먼저 막는다."""
    total = 0
    try:
        with destination.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "업로드 파일 크기가 상한을 넘었습니다.")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return total


@app.post("/admin/repos/upload", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_repo(file: UploadFile = File(...), repo: str = Form(...),
                            sensitive: str = Form("true"), replace: str = Form("false")):
    """zip으로 저장소를 올린다. 새 이름이면 등록(기본 민감), replace=true면 업로드로 등록했던 저장소만 교체 후 재분석."""
    import tempfile
    from backend.app.security.repo_upload import Limits
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        await _copy_upload_limited(file, tmp_path, Limits().max_zip_bytes)
        return await _install_zip(tmp_path, repo, _flag(sensitive, True), _flag(replace, False), "zip")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/admin/repos/upload-folder", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_folder(files: list[UploadFile] = File(...), repo: str = Form(...),
                              sensitive: str = Form("true"), replace: str = Form("false")):
    """브라우저에서 폴더를 통째로 고른 경우. 파일 이름(상대 경로)을 그대로 zip으로 묶은 뒤 zip과 같은 검사를 거친다."""
    import tempfile
    import zipfile
    from backend.app.security.repo_upload import Limits
    lim = Limits()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    total = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if len(files) > lim.max_files * 2:
                raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "업로드 파일 수가 상한을 넘습니다.")
            for f in files:
                file_total = 0
                # ZipFile.open은 스트리밍 쓰기를 지원한다. 대형 폴더 업로드를 메모리에 올리지 않는다.
                with zf.open(f.filename or "unnamed", "w") as out:
                    while chunk := await f.read(1024 * 1024):
                        file_total += len(chunk)
                        total += len(chunk)
                        if file_total > lim.max_file_bytes or total > lim.max_total_bytes:
                            raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "업로드 폴더 크기가 상한을 넘습니다.")
                        out.write(chunk)      # 경로 검사는 extract_repo가 한다
        return await _install_zip(tmp_path, repo, _flag(sensitive, True), _flag(replace, False), "folder")
    finally:
        tmp_path.unlink(missing_ok=True)


DOC_EXT = {".md", ".markdown", ".txt", ".rst"}


@app.get("/admin/repos/{repo}/docs", dependencies=[Depends(require_admin)])
async def admin_list_docs(repo: str):
    from backend.app.security.path_guard import docs_dir
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    files = sorted(({"name": p.name, "bytes": p.stat().st_size} for p in d.iterdir() if p.is_file() and not p.is_symlink()),
                   key=lambda x: x["name"]) if d.is_dir() else []
    return {"repo": repo, "uploaded": files}


@app.post("/admin/repos/{repo}/docs", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_upload_docs(repo: str, files: list[UploadFile] = File(...)):
    """저장소에 문서(기획서·명세·README 등 md·txt·rst)를 따로 올린다. 저장소 코드 폴더는 건드리지 않고
    var/data/docs/<repo>/ 에 저장하며, 문서 검색·문서-코드 대조에 바로 반영된다."""
    from backend.app.security.path_guard import docs_dir
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    saved, total = [], 0
    for f in files:
        name = Path((f.filename or "").replace("\\", "/")).name
        if not name or name.startswith(".") or len(name) > 120 or "\x00" in name or Path(name).suffix.lower() not in DOC_EXT:
            raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "문서는 md·txt·rst 형식만 올릴 수 있습니다.")
        chunks, file_total = [], 0
        while chunk := await f.read(64 * 1024):
            file_total += len(chunk)
            total += len(chunk)
            if file_total > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
                raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "문서 크기 상한을 넘었습니다.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if file_total > 2 * 1024 * 1024 or total > 20 * 1024 * 1024:
            raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "문서 크기 상한을 넘었습니다.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise AppError(ErrorCode.UPLOAD_REJECTED, 400, "UTF-8 텍스트 문서만 올릴 수 있습니다.")
        saved.append((name, data))
    d.mkdir(parents=True, exist_ok=True)
    for name, data in saved:
        (d / name).write_bytes(data)
    registry.bump(repo)
    await app.state.deps.mcp.call_tool("reanalyze", {"repo": repo})     # 문서 색인 갱신
    audit.record("docs_upload", repo=repo, files=len(saved), bytes=total)
    return {**(await _repo_row(repo)), "uploaded_docs": [n for n, _ in saved]}


@app.delete("/admin/repos/{repo}/docs/{name}", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_delete_doc(repo: str, name: str):
    """업로드한 문서만 지운다(저장소 안 문서는 지울 수 없다)."""
    from backend.app.security.path_guard import docs_dir, safe_file
    registry.apply(_POLICY)
    require_repo(repo, _POLICY.allowed_repos)
    d = docs_dir(repo, _POLICY.allowed_repos)
    try:
        p = safe_file(d, name) if d.is_dir() else None
    except ValueError:
        p = None
    if p is None or p == d or p.parent != d or not p.is_file():
        raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, 404, "문서를 찾을 수 없습니다.")
    p.unlink()
    registry.bump(repo)
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


class DeleteRepo(BaseModel):
    confirm_name: str


@app.delete("/admin/repos/{repo}", dependencies=[Depends(require_admin), Depends(_admin_write)])
async def admin_delete_repo(repo: str, req: DeleteRepo):
    """관리자 확인 후 런타임 저장소 전체 삭제. 정책 기본 저장소는 변경하지 않는다."""
    from backend.app.security.policy import load_policy
    from backend.app.repositories import deletion
    if repo in load_policy().allowed_repos:
        raise AppError(ErrorCode.REPO_PROTECTED, 400, "정책 기본 저장소는 삭제할 수 없습니다.")
    if req.confirm_name != repo:
        raise AppError(ErrorCode.REQUEST_INVALID, 400, "확인용 저장소 이름이 일치하지 않습니다.")
    if not registry._NAME.fullmatch(repo) or repo in (".", ".."):
        raise AppError(ErrorCode.REQUEST_INVALID, 400, "저장소 이름 형식이 올바르지 않습니다.")
    try:
        token, files, fresh = deletion.begin(repo)
    except deletion.DeletionError as e:
        code, status, message = {
            "not_found": (ErrorCode.REPO_NOT_FOUND, 404, "저장소를 찾을 수 없습니다."),
            "protected": (ErrorCode.REPO_PROTECTED, 400, "정책 기본 저장소는 삭제할 수 없습니다."),
            "unsafe_path": (ErrorCode.REQUEST_INVALID, 400, "안전하지 않은 저장소 경로입니다."),
        }[e.kind]
        raise AppError(code, status, message) from None
    registry.forget_policy(_POLICY, repo)
    try:
        if fresh:
            deletion.stage(repo, token)
        await app.state.deps.mcp.call_tool("forget_repo", {"repo": repo})
    except Exception:
        if fresh:
            deletion.rollback(repo, token)
            registry.apply(_POLICY)
        raise
    try:
        await asyncio.to_thread(deletion.finish, repo, token, files)
    except Exception:
        raise AppError(ErrorCode.DELETION_PENDING, 503, "저장소 삭제가 진행 중입니다. 잠시 뒤 다시 시도해 주세요.") from None
    return {"repo": repo, "files": files}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
