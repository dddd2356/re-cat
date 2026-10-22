"""트러블슈팅 회귀 테스트 — TROUBLESHOOTING.md의 ①~⑧을 실제로 일으켜 보고 고쳐진 동작을 확인한다."""
import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.agents import llm_common
from backend.app.agents.llm_common import LLMUnavailable, post_json
from backend.app.agents.verifier import verify

ROOT = Path(__file__).parent.parent
KIT = ROOT / "tests" / "fixtures"


# ---- ③ LLM HTTP 재시도 ----
class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}
    def json(self):
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)


def _fake_post(codes):
    calls = []
    def post(url, json=None, timeout=None, headers=None):
        calls.append(1)
        c = codes[min(len(calls) - 1, len(codes) - 1)]
        if c == "timeout":
            raise httpx.ReadTimeout("slow")
        return _Resp(c, {"ok": True})
    return post, calls


def test_post_json_retries_transient_then_succeeds(monkeypatch):
    post, calls = _fake_post([503, "timeout", 200])
    monkeypatch.setattr(llm_common._httpx, "post", post)
    assert post_json("u", {}, timeout=1, base_delay=0) == {"ok": True} and len(calls) == 3


def test_post_json_gives_up_with_llm_unavailable(monkeypatch):
    post, calls = _fake_post([503])
    monkeypatch.setattr(llm_common._httpx, "post", post)
    with pytest.raises(LLMUnavailable):
        post_json("u", {}, timeout=1, base_delay=0)
    assert len(calls) == 3


def test_post_json_does_not_retry_client_errors(monkeypatch):
    post, calls = _fake_post([400])
    monkeypatch.setattr(llm_common._httpx, "post", post)
    with pytest.raises(httpx.HTTPStatusError):
        post_json("u", {}, timeout=1, base_delay=0)
    assert len(calls) == 1


# ---- ⑤⑥ 검증기 ----
CHAIN = {"pairs": [("OrderService.create", "OrderService.validate")], "unresolved": [],
         "known_symbols": ["OrderService.create", "OrderService.validate", "StockService.decrease"]}
OK = [{"caller": "OrderService.create", "callee": "OrderService.validate"}]


def test_empty_explanation_or_claims_is_not_a_pass():
    assert not verify([], CHAIN, text="", require_claims=True).ok
    assert not verify([], CHAIN, text="설명은 있는데 주장이 없음", require_claims=True).ok
    assert verify([], CHAIN).ok                                        # 문서 경로(주장 없음이 정상)는 그대로


def test_name_only_in_prose_is_caught():
    v = verify(OK, CHAIN, text="create가 validate를 부르고, AuditLogger.write로 기록합니다.", require_claims=True)
    assert not v.ok and v.hallucinated == [{"caller": "(설명 본문)", "callee": "AuditLogger.write"}]
    # 파일 이름·self 경유·인스턴스 변수 경유 호출은 오탐하지 않는다
    t = "orders/service.py의 create는 self.stock.decrease와 stock.decrease를 거칩니다. `OrderService.validate`도."
    assert verify(OK, CHAIN, text=t, require_claims=True).ok


# ---- ⑦⑧ 업로드 ----
def _zip(tmp_path, name, files):
    import zipfile
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return p


def test_non_replace_upload_never_overwrites_a_repo_that_appeared_meanwhile(tmp_path, monkeypatch):
    from backend.app.security import repo_upload
    root = tmp_path / "repos"; root.mkdir()
    z = _zip(tmp_path, "b.zip", {"b.py": "def b(): pass\n"})
    real = repo_upload._members
    def members_then_race(zf, lim):                    # 첫 확인 직후 다른 요청이 같은 이름을 설치한 상황
        (root / "r").mkdir(); (root / "r" / "a.py").write_text("def a(): pass\n")
        return real(zf, lim)
    monkeypatch.setattr(repo_upload, "_members", members_then_race)
    with pytest.raises(repo_upload.UploadRejected):
        repo_upload.extract_repo(z, "r", root)
    assert sorted(p.name for p in (root / "r").iterdir()) == ["a.py"]      # 먼저 온 쪽 파일이 그대로
    assert not [p for p in root.iterdir() if p.name.startswith(".upload-")]  # 임시 폴더도 정리


def test_failed_swap_restores_previous_repo(tmp_path, monkeypatch):
    from backend.app.security import repo_upload
    root = tmp_path / "repos"; root.mkdir()
    repo_upload.extract_repo(_zip(tmp_path, "a.zip", {"a.py": "x=1\n"}), "r", root)
    orig = Path.rename
    def flaky(self, target):
        if self.name.startswith(".upload-"):
            raise OSError(28, "No space left on device")
        return orig(self, target)
    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError):
        repo_upload.extract_repo(_zip(tmp_path, "b.zip", {"b.py": "x=2\n"}), "r", root, replace=True)
    monkeypatch.setattr(Path, "rename", orig)
    assert (root / "r" / "a.py").exists() and not (root / ".replaced-r").exists()


# ---- ② MCP 서브프로세스 멈춤·죽음 ----
def _child_pids():
    if os.name == "nt":
        # pgrep은 Unix 전용이다. MCP stdio 프로세스는 테스트 Python의 직접 자식이므로
        # Windows의 CIM 프로세스 목록에서 같은 조건으로 찾는다.
        command = (
            f"Get-CimInstance Win32_Process -Filter 'ParentProcessId = {os.getpid()}' | "
            "Where-Object { $_.CommandLine -match 'mcp[\\\\/]server\\.py' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True).stdout
        return [int(x) for x in out.split()]
    # MCP 서버가 루트 mcp_server.py였던 구버전과 backend/app/mcp/server.py인 현재 버전을 모두 찾는다.
    out = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", r"mcp(_|/)server\.py"], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def test_mcp_client_recovers_from_killed_and_frozen_subprocess(monkeypatch):
    monkeypatch.setenv("RECAT_MCP_TIMEOUT", "3")
    from backend.app.agents.mcp_client import MCPClient
    from backend.app.agents.retry import with_retry

    async def run():
        c = MCPClient()
        await c.start()
        try:
            r, _ = await with_retry(c.call_tool, "list_functions", {"repo": "sample_py"})
            assert r["count"] > 0
            (pid,) = _child_pids()
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True, capture_output=True)
            else:
                os.kill(pid, signal.SIGKILL)
            await asyncio.sleep(0.5)                                            # 죽음
            r, retries = await with_retry(c.call_tool, "list_functions", {"repo": "sample_py"})
            assert r["count"] > 0 and retries >= 1 and c.restarts == 1
            if os.name == "nt":
                return  # Windows에는 SIGSTOP/SIGCONT에 해당하는 안전한 표준 API가 없다.
            (pid2,) = _child_pids(); assert pid2 != pid
            os.kill(pid2, signal.SIGSTOP)                                       # 멈춤(응답 없음)
            t0 = time.time()
            r, retries = await with_retry(c.call_tool, "list_functions", {"repo": "sample_py"})
            assert r["count"] > 0 and c.restarts == 2 and time.time() - t0 < 20
        finally:
            if os.name != "nt":
                for p in _child_pids():
                    try: os.kill(p, signal.SIGCONT)
                    except ProcessLookupError: pass
            await c.stop()
    asyncio.run(run())


# ---- ③④ 서버: 모델이 끝내 없을 때 / 처리 중 예외 ----
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    with TestClient(server.app) as c:
        yield c


def test_llm_down_falls_back_to_structure_only(client, monkeypatch):
    deps = client.app.state.deps
    async def down(*a, **k):
        raise LLMUnavailable("모델 서버 응답 없음 (HTTP 503, 3회 시도)")
    monkeypatch.setattr(deps.quality_llm, "explain", down)
    r = client.post("/chat", json={"message": "OrderService.cancel 호출 흐름", "repo": "sample_py"})
    assert r.status_code == 200
    d = r.json()
    assert d["metadata"]["mode"] == "no-llm-fallback"
    assert "모델 서버가 응답하지 않아 정적 분석 결과만" in d["reply"]
    assert "OrderService.cancel → PaymentClient.refund" in d["reply"]
    assert any(e["type"] == "Fallback" and e["reason"] == "llm_unavailable" for e in d["events"])


def test_unexpected_error_is_reported_not_dropped(client, monkeypatch):
    import server
    async def boom(*a, **k):
        raise RuntimeError("unexpected")
        yield  # pragma: no cover
    monkeypatch.setattr(client.app.state.graph, "astream", boom)
    body = client.get("/chat/stream", params={"message": "OrderService.cancel 호출 흐름", "repo": "sample_py"}).text
    events = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: {")]
    assert events and events[-1]["type"] == "Error" and "RuntimeError" in events[-1]["error"]["message"]
    assert body.rstrip().endswith("data: [DONE]")
    assert server._read_audit()[-1]["event"] == "chat_error"
    async def boom2(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(client.app.state.graph, "ainvoke", boom2)
    r = client.post("/chat", json={"message": "OrderService.cancel 호출 흐름", "repo": "sample_py"})
    assert r.status_code == 503 and "RuntimeError" in r.json()["error"]["message"]


# ---- ① 느린 모델 호출이 이벤트 루프를 막지 않는다 ----
def test_slow_llm_call_does_not_block_event_loop(monkeypatch):
    from backend.app.agents import ollama_llm
    def slow_post(url, body, timeout, headers=None):
        time.sleep(1.0)                                 # 모델이 1초 걸린다(동기 HTTP)
        return {"message": {"content": json.dumps({"explanation": "x", "claims": []})},
                "prompt_eval_count": 1, "eval_count": 1}
    monkeypatch.setattr(ollama_llm, "post_json", slow_post)
    llm = ollama_llm.OllamaQualityLLM("m")

    async def run():
        ticks = []
        async def heartbeat():                          # 다른 요청(/health 등) 대신 0.05초마다 깨어나는 태스크
            t0 = time.time()
            while time.time() - t0 < 1.0:
                ticks.append(time.time()); await asyncio.sleep(0.05)
        await asyncio.gather(llm.explain("q", {"pairs": [], "unresolved": []}, {}, 1, None), heartbeat())
        return max(b - a for a, b in zip(ticks, ticks[1:]))
    assert asyncio.run(run()) < 0.3                     # 막혔다면 최대 간격이 ~1초


# ---- ⑩ 문서 후보를 LLM에 주되, 명확한 코드 질문의 잡담 오판은 안전망으로 막는다 ----
def test_route_uses_model_but_keeps_code_question_fallback(client, monkeypatch):
    deps = client.app.state.deps
    calls = []
    def always_chat(q, document_candidates=None):
        calls.append(q)
        return {"route": "chat"}, {"usd": 0.0}
    monkeypatch.setattr(deps.fast_llm, "route", always_chat)
    for q in ["주문 생성은 어떻게 동작해?", "OrderService.create 호출 흐름", "run_hook은 무엇을 호출해?"]:
        d = client.post("/chat", json={"message": q, "repo": "sample_py", "thread_id": "rt" + q}).json()
        rd = next(e for e in d["events"] if e["type"] == "RouteDecided")
        assert rd["route"] == "trace" and rd["by"] == "rule-fallback" and d["metadata"]["mode"] in ("agent", "no-llm-fallback")
    assert calls == ["주문 생성은 어떻게 동작해?", "OrderService.create 호출 흐름", "run_hook은 무엇을 호출해?"]
    d = client.post("/chat", json={"message": "안녕", "repo": "sample_py", "thread_id": "rt-hi"}).json()
    assert next(e for e in d["events"] if e["type"] == "RouteDecided")["by"] == "model" and calls[-1] == "안녕"


# ---- 규칙이 trace로 확신한 질문을, 모델이 doc으로 끌어내려도 안 된다 (예: "_doc_files 호출 흐름") ----
def test_route_model_cannot_downgrade_confident_trace_rule_to_doc(client, monkeypatch):
    deps = client.app.state.deps
    calls = []
    def always_doc(q, document_candidates=None):
        calls.append(q)
        return {"route": "doc"}, {"usd": 0.0}
    monkeypatch.setattr(deps.fast_llm, "route", always_doc)
    for q in ["OrderService.create 호출 흐름", "run_hook은 무엇을 호출해?"]:
        d = client.post("/chat", json={"message": q, "repo": "sample_py", "thread_id": "rtdoc" + q}).json()
        rd = next(e for e in d["events"] if e["type"] == "RouteDecided")
        assert rd["route"] == "trace" and rd["by"] == "rule-fallback"
    assert calls == ["OrderService.create 호출 흐름", "run_hook은 무엇을 호출해?"]
