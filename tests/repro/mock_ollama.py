"""모의 Ollama — /api/chat. 동작은 mode.json으로 바꾼다(재시작 없이).
mode: delay(초), fail(앞으로 N번 503), bad_json(true면 깨진 JSON), fake_name(설명 본문에만 없는 함수 이름)"""
import asyncio, json, re, sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
MODE = Path(__file__).parent / "mode.json"
app = FastAPI()
def mode():
    try: return json.loads(MODE.read_text())
    except Exception: return {}
@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json(); m = mode()
    if m.get("fail", 0) > 0:
        m["fail"] -= 1; MODE.write_text(json.dumps(m))
        return JSONResponse({"error": "model is loading"}, status_code=503)
    await asyncio.sleep(m.get("delay", 0))
    system = body["messages"][0]["content"]; user = body["messages"][1]["content"]
    if "route" in json.dumps(body.get("format", {})):
        out = {"route": m.get("route", "trace")}
    elif "expand" in json.dumps(body.get("format", {})):
        cands = user.split("[후보]\n", 1)[1].splitlines() if "[후보]" in user else []
        out = {"expand": cands}
    elif body.get("format"):
        pairs = re.findall(r"^(\S+) -> (\S+)$", user, re.M)
        text = "요약: " + ", ".join(f"{a}가 {b}를 호출" for a, b in pairs)
        if m.get("fake_name"):
            text += ". 마지막에 AuditLogger.write로 감사 로그를 남깁니다."   # claims에는 넣지 않는다
        out = {"explanation": text, "claims": [{"caller": a, "callee": b} for a, b in pairs]}
    else:
        return {"message": {"content": "안녕하세요"}, "prompt_eval_count": 1, "eval_count": 1}
    content = "{\"explanation\": \"잘리" if m.get("bad_json") and body.get("format") and "explanation" in json.dumps(body["format"]) else json.dumps(out, ensure_ascii=False)
    return {"message": {"content": content}, "prompt_eval_count": 10, "eval_count": 10}
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 11999, log_level="warning")
