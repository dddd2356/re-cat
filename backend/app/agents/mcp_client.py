"""MCP 클라이언트 — MCP 서버를 subprocess(stdio)로 1회 기동해 재사용한다 (Base의 mcp_client 유지, 서버 구성만 변경).

lifespan에서 한 번만 연결을 만들고 모든 요청이 공유한다.
트러블슈팅 ②: 서브프로세스가 멈추거나(응답 없음) 죽으면(연결 끊김) 예전에는 요청이 영원히 기다리거나
서버를 재시작할 때까지 모든 질문이 500이었다. 이제
  - 모든 호출에 시간 제한(RECAT_MCP_TIMEOUT, 기본 60초; 분석을 새로 할 수 있는 관리자 도구는 600초)을 걸고
  - 시간 초과·연결 끊김이면 서브프로세스를 다시 띄운 뒤 TransientError를 올린다 → with_retry가 재시도한다.
  - 연결은 전용 태스크가 소유한다(anyio 컨텍스트는 연 태스크에서 닫아야 하므로 요청 태스크에서 닫지 않는다). RECAT_FLAKY=1이면 프로세스 최초의
get_callees 호출 1회가 일부러 TimeoutError를 내, with_retry가 복구하는 경로를 확인할 수 있다.
"""
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from langsmith import traceable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .retry import TransientError

log = logging.getLogger("recat.mcp")
SLOW_TOOLS = {"reanalyze", "get_status"}          # 저장소를 처음 분석할 수 있는 도구 — 시간 제한을 길게


def _is_transport_error(e: BaseException) -> bool:
    """도구 자체 오류(isError 결과)가 아니라 연결이 끊긴 것인가."""
    if isinstance(e, (asyncio.TimeoutError, EOFError, BrokenPipeError, ConnectionError)):
        return True
    name = type(e).__name__
    return name in {"ClosedResourceError", "BrokenResourceError", "EndOfStream"} or \
        "Connection closed" in str(e) or "connection closed" in str(e).lower()

APP_DIR = Path(__file__).parent.parent
DEFAULT_SERVER_SCRIPTS = {
    "code": APP_DIR / "mcp" / "server.py",   # Recat 코드 분석 MCP 서버 (별도 subprocess)
}


def _attr(obj, *names):
    """mcp SDK 버전에 따라 pydantic 필드명이 camelCase(예: resourceTemplates,
    uriTemplate, 구버전 1.x)와 snake_case(예: resource_templates, uri_template,
    신버전 2.x)로 갈린다 — 이 프로젝트 안에도 시스템 python3(mcp 1.28.1, camelCase)와
    .venv(mcp 2.2.0, snake_case)가 섞여 있어 실행 환경마다 바뀐다. 둘 다 시도해서
    설치된 버전과 무관하게 동작하게 한다."""
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AttributeError(f"{type(obj).__name__!r} has none of {names!r}")


class MCPClient:
    def __init__(self, flaky_first_tool_call: bool = False,
                 server_scripts: dict[str, Path] | None = None):
        self._server_scripts = server_scripts or DEFAULT_SERVER_SCRIPTS
        self._stack: AsyncExitStack | None = None
        self.sessions: dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()
        self._flaky_pending = flaky_first_tool_call   # Wave 9 시뮬레이션
        self.timeout = float(os.environ.get("RECAT_MCP_TIMEOUT", "60"))
        self.generation = 0          # 서브프로세스를 다시 띄울 때마다 1씩 증가
        self.restarts = 0
        self._owner: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._closing = False
        self._start_error: BaseException | None = None
        self._restart_lock = asyncio.Lock()

    async def _own(self) -> None:
        """연결을 열고, 닫으라는 신호가 올 때까지 쥐고 있다가 같은 태스크에서 닫는다. 재시작이면 다시 연다."""
        while not self._closing:
            self._stop = asyncio.Event()
            try:
                async with AsyncExitStack() as stack:
                    sessions = {}
                    for server_name, script in self._server_scripts.items():
                        params = StdioServerParameters(
                            command=sys.executable, args=[str(script)],
                            env={**os.environ, "FASTMCP_LOG_LEVEL": "ERROR"})
                        read, write = await stack.enter_async_context(stdio_client(params))
                        session = await stack.enter_async_context(ClientSession(read, write))
                        await asyncio.wait_for(session.initialize(), 30)
                        sessions[server_name] = session
                    self.sessions, self._start_error = sessions, None
                    self.generation += 1
                    self._ready.set()
                    await self._stop.wait()
                    self.sessions = {}
            except Exception as e:            # 기동 실패 — 기다리는 쪽에 알리고 잠시 뒤 다시 시도
                self._start_error = e
                self.sessions = {}
                self._ready.set()
                if not self._closing:
                    await asyncio.sleep(1.0)

    async def start(self) -> None:
        self._ready.clear()
        self._owner = asyncio.create_task(self._own())
        await self._ready.wait()
        if self._start_error is not None:
            raise RuntimeError(f"MCP 서버 기동 실패: {self._start_error}")

    async def stop(self) -> None:
        self._closing = True
        self._stop.set()
        if self._owner is not None:
            try:
                await asyncio.wait_for(self._owner, 15)
            except (asyncio.TimeoutError, Exception):
                self._owner.cancel()
            self._owner = None
        self.sessions = {}

    async def _restart(self, seen_generation: int, why: str) -> None:
        """멈추거나 죽은 서브프로세스를 다시 띄운다. 여러 요청이 동시에 실패해도 한 번만 재시작한다."""
        async with self._restart_lock:
            if self.generation != seen_generation:
                return                        # 다른 요청이 이미 다시 띄웠다
            log.warning("MCP 서브프로세스 재시작: %s", why)
            self.restarts += 1
            self._ready.clear()
            self._stop.set()                  # 소유 태스크가 연결을 닫는다(멈춘 프로세스는 SDK가 kill까지 한다)
            try:
                await asyncio.wait_for(self._ready.wait(), 45)
            except asyncio.TimeoutError:
                pass

    async def _rpc(self, label: str, server: str, make, timeout: float | None = None):
        """세션 호출 공통: 시간 제한 + 연결 오류면 재시작 후 TransientError."""
        gen = self.generation
        try:
            async with self._lock:
                session = self.sessions.get(server)
                if session is None:
                    raise ConnectionError("MCP 세션 없음(재시작 중)")
                return await asyncio.wait_for(make(session), timeout or self.timeout)
        except Exception as e:
            if not _is_transport_error(e):
                raise
            await self._restart(gen, f"{label}: {type(e).__name__}")
            raise TransientError(f"MCP {label}: {type(e).__name__} — 서브프로세스를 다시 띄웠습니다") from e

    @traceable(run_type="tool", name="mcp.call_tool")
    async def call_tool(self, name: str, arguments: dict, server: str = "code") -> dict:
        if self._flaky_pending and name == "get_callees":
            self._flaky_pending = False
            raise asyncio.TimeoutError("simulated transient timeout (retry demo)")
        result = await self._rpc(name, server, lambda s: s.call_tool(name, arguments),
                                 timeout=max(self.timeout, 600.0) if name in SLOW_TOOLS else None)
        # MCP SDK v2는 is_error를 쓴다. 먼저 최신 필드를 읽어 deprecation 경고를 피하고,
        # 구버전 SDK에서만 camelCase 필드로 호환한다.
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            raise RuntimeError(f"MCP tool {name} failed: {result.content[0].text[:200]}")
        return json.loads(result.content[0].text)

    @traceable(run_type="tool", name="mcp.list_resource_templates")
    async def list_resource_templates(self, server: str) -> list:
        result = await self._rpc("list_resource_templates", server, lambda s: s.list_resource_templates())
        return _attr(result, "resourceTemplates", "resource_templates")

    @traceable(run_type="tool", name="mcp.find_resource_template")
    async def find_resource_template(self, template_name: str) -> tuple[str, str]:
        """연결된 서버들을 순서대로 조회해 template_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            templates = await self.list_resource_templates(server_name)
            matched = next((t for t in templates if t.name == template_name), None)
            if matched is not None:
                return server_name, _attr(matched, "uriTemplate", "uri_template")
        raise ValueError(
            f"MCP resource template not found on any connected server: {template_name}")

    @traceable(run_type="tool", name="mcp.read_resource")
    async def read_resource(self, uri: str, server: str) -> str:
        result = await self._rpc("read_resource", server, lambda s: s.read_resource(uri))
        return result.contents[0].text

    @traceable(run_type="tool", name="mcp.list_prompts")
    async def list_prompts(self, server: str) -> list:
        result = await self._rpc("list_prompts", server, lambda s: s.list_prompts())
        return result.prompts

    @traceable(run_type="tool", name="mcp.find_prompt")
    async def find_prompt(self, prompt_name: str) -> str:
        """연결된 서버들을 순서대로 조회해 prompt_name을 제공하는 서버를 찾는다."""
        for server_name in self.sessions:
            prompts = await self.list_prompts(server_name)
            if any(p.name == prompt_name for p in prompts):
                return server_name
        raise ValueError(
            f"MCP prompt not found on any connected server: {prompt_name}")

    @traceable(run_type="tool", name="mcp.get_prompt")
    async def get_prompt(self, name: str, arguments: dict, server: str = "code") -> str:
        result = await self._rpc("get_prompt", server, lambda s: s.get_prompt(name, arguments))
        return result.messages[0].content.text
