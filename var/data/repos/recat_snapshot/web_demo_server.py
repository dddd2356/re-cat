"""web_demo_server.py — Recat 에이전트와 브라우저 채팅창으로 대화해보는 데모 (Base 프록시 유지).

web_chat.html이 바닐라 JS 대신 Vue 3(CDN 글로벌 빌드, 별도 빌드 단계 없음)로 되어
있다. server.py(8100)는 전혀
건드리지 않는다. 이 스크립트는
- web_chat.html 한 장을 같은 출처(origin)로 띄우고
- /chat/stream 요청을 그대로 8100으로 중계(프록시)해서

브라우저의 CORS 제약 없이 실제 에이전트와 대화할 수 있게 해주는 아주 얇은 레이어다.
server.py는 평소처럼(fake/ollama/openai 아무 백엔드로나) 8100에 미리 띄워 놓아야 한다.

실행:
    python3 web_demo_server.py        # http://localhost:8600 에서 채팅 화면 오픈
"""
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import os

from dotenv import load_dotenv

load_dotenv()   # server.py와 같은 .env의 RECAT_API_KEY를 쓴다

BACKEND = "http://127.0.0.1:8100"
# 브라우저의 EventSource는 헤더를 못 붙이므로, RECAT_API_KEY가 있으면 프록시가 서버 쪽에서 붙인다.
AUTH = {"Authorization": f"Bearer {os.environ['RECAT_API_KEY']}"} if os.environ.get("RECAT_API_KEY") else {}
HERE = Path(__file__).parent

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(HERE / "web_chat.html")


@app.get("/web_graph.js")
async def graph_js():
    return FileResponse(HERE / "web_graph.js", media_type="application/javascript")


@app.get("/admin")
async def admin_page():
    return FileResponse(HERE / "web_admin.html")


@app.get("/admin/summary")
async def proxy_admin_summary(authorization: str | None = Header(default=None)):
    """관리자 키는 프록시가 붙이지 않는다 — 브라우저가 입력받은 키를 그대로 전달해 서버가 판정한다."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/admin/summary",
                             headers={"Authorization": authorization} if authorization else {})
    return JSONResponse(r.json(), status_code=r.status_code)


async def _admin_forward(method: str, path: str, authorization, body: bytes = b"",
                         content_type: str = "application/json"):
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.request(method, f"{BACKEND}{path}", content=body or None,
                                 headers={**({"Authorization": authorization} if authorization else {}),
                                          "Content-Type": content_type})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/admin/repos")
async def proxy_admin_repos(authorization: str | None = Header(default=None)):
    return await _admin_forward("GET", "/admin/repos", authorization)


@app.post("/admin/repos")
async def proxy_admin_register(req: Request, authorization: str | None = Header(default=None)):
    return await _admin_forward("POST", "/admin/repos", authorization, await req.body())


@app.post("/admin/repos/upload")
async def proxy_admin_upload(req: Request, authorization: str | None = Header(default=None)):
    """zip 업로드는 multipart 본문을 그대로 넘긴다(프록시는 내용을 보지 않는다)."""
    return await _admin_forward("POST", "/admin/repos/upload", authorization, await req.body(),
                                req.headers.get("content-type", ""))


@app.post("/admin/repos/upload-folder")
async def proxy_admin_upload_folder(req: Request, authorization: str | None = Header(default=None)):
    return await _admin_forward("POST", "/admin/repos/upload-folder", authorization, await req.body(),
                                req.headers.get("content-type", ""))


@app.get("/admin/repos/{repo}/docs")
async def proxy_admin_docs(repo: str, authorization: str | None = Header(default=None)):
    return await _admin_forward("GET", f"/admin/repos/{repo}/docs", authorization)


@app.post("/admin/repos/{repo}/docs")
async def proxy_admin_docs_upload(repo: str, req: Request, authorization: str | None = Header(default=None)):
    return await _admin_forward("POST", f"/admin/repos/{repo}/docs", authorization, await req.body(),
                                req.headers.get("content-type", ""))


@app.delete("/admin/repos/{repo}/docs/{name}")
async def proxy_admin_docs_delete(repo: str, name: str, authorization: str | None = Header(default=None)):
    return await _admin_forward("DELETE", f"/admin/repos/{repo}/docs/{name}", authorization)


@app.post("/admin/repos/{repo}/reanalyze")
async def proxy_admin_reanalyze(repo: str, authorization: str | None = Header(default=None)):
    return await _admin_forward("POST", f"/admin/repos/{repo}/reanalyze", authorization)


@app.get("/graph/{repo}")
async def proxy_graph(repo: str):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/graph/{repo}", headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/group/{repo}")
async def proxy_group(repo: str, id: str):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/group/{repo}", params={"id": id}, headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/flows/{repo}")
async def proxy_flows(repo: str, symbol: str, depth: int = 3):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/flows/{repo}", params={"symbol": symbol, "depth": depth}, headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/source/{repo}")
async def proxy_source(repo: str, symbol: str):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/source/{repo}", params={"symbol": symbol}, headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/file/{repo}")
async def proxy_file(repo: str, symbol: str = "", path: str = ""):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/file/{repo}", params={"symbol": symbol, "path": path}, headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/callers/{repo}")
async def proxy_callers(repo: str, symbol: str, depth: int = 3):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BACKEND}/callers/{repo}", params={"symbol": symbol, "depth": depth}, headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/health")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/repos")
async def proxy_repos():
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BACKEND}/repos", headers=AUTH)
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/chat")
async def proxy_chat(req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BACKEND}/chat", content=body,
                              headers={"content-type": "application/json", **AUTH})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/chat/stream")
async def proxy_stream(message: str, thread_id: str = "web-demo", repo: str = "sample_py"):
    async def relay():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", f"{BACKEND}/chat/stream",
                params={"message": message, "thread_id": thread_id, "repo": repo},
                headers=AUTH,
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")


if __name__ == "__main__":
    print("브라우저에서 http://localhost:8600 을 열어보세요 (server.py가 8100에 떠 있어야 합니다)")
    uvicorn.run(app, host="127.0.0.1", port=8600)
