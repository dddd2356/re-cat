"""비교 실험: 호출 관계 주장의 오류율을 세 방식으로 비교한다.

  A  baseline  — 저장소 코드만 주고 설명·주장(claims)을 받는다 (검증 없음, 일반 LLM 사용 방식)
  B  structure — 정적 분석 구조를 함께 주지만 검증하지 않는다 (Recat 첫 시도)
  C  recat     — 검증·재설명·폴백까지 포함한 Recat 전체

채점 기준(정답)은 진입점에서 깊이 6까지의 정적 분석 호출 체인이다. A는 질문마다 정한 진입점, B·C는 Recat이
실제로 고른 진입점 기준이다(진입점 검색이 틀린 경우는 '진입점 적중률'로 따로 보고한다).
  verified      체인에 있는 관계
  hallucinated  저장소에 없는 이름
  wrong         이름은 있지만 체인에도, 분석이 못 따라간 호출에도 없는 관계
  unverifiable  분석이 못 따라간 호출과 겹쳐 판정 불가 (오류로 세지 않음)
오류율 = (hallucinated + wrong) / 전체 주장.  C가 폴백하면 LLM 주장이 없으므로 오류율 계산에서 빠지고
폴백 비율로 따로 보고한다 (오류가 없는 것이 아니라 '설명을 포기한 것'이므로 숨기지 않는다).

실행:  python -m eval.run_eval [--backend ollama] [--arms A,B,C] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.analyzers.loader import analyze_repo          # noqa: E402
from backend.app.agents.llm_common import EXPLAIN_SCHEMA        # noqa: E402
from backend.app.agents.verifier import verify                  # noqa: E402

BASELINE_SYSTEM = (
    "당신은 코드 호출 흐름을 설명하는 도우미입니다. 아래 코드를 읽고 질문에 답하세요. "
    "explanation에는 한국어 설명을, claims에는 설명에서 주장한 모든 호출 관계를 "
    "{caller, callee} 쌍(함수의 qualname, 예: OrderService.create)으로 담으세요. JSON만 출력하세요.")
PER_FILE_CHARS = 6000


def ground_truth(graph, entry: str, depth: int = 6) -> dict:
    s = graph.find(entry)[0]
    q = lambda sid: graph.symbols[sid].qualname
    return {"pairs": sorted((q(a), q(b)) for a, b in graph.resolved_pairs(s.id, depth)),
            "unresolved": [{"caller": q(u["caller"]), "text": u["text"]}
                           for u in graph.unresolved_texts(s.id, depth)],
            "known_symbols": sorted({x.qualname for x in graph.symbols.values()})}


def repo_code(repo: str) -> str:
    root = ROOT / "var" / "data" / "repos" / repo
    parts = []
    for p in sorted(root.rglob("*")):
        if p.suffix in (".py", ".java") and p.is_file() and "__pycache__" not in p.parts:
            parts.append(f"### {p.relative_to(root).as_posix()}\n{p.read_text(encoding='utf-8')[:PER_FILE_CHARS]}")
    return "\n\n".join(parts)


def score(claims: list[dict], truth: dict) -> dict:
    v = verify(claims, truth).as_dict()
    n = v["verified"] + v["hallucinated"] + v["wrong"] + v["unverifiable"]
    return {"claims": n, "verified": v["verified"], "hallucinated": v["hallucinated"],
            "wrong": v["wrong"], "unverifiable": v["unverifiable"]}


def raw_json(llm, system: str, user: str) -> dict:
    fn = llm._json
    out = fn(system, user, EXPLAIN_SCHEMA, "baseline") if len(inspect.signature(fn).parameters) >= 4 \
        else fn(system, user, EXPLAIN_SCHEMA)
    return out[0]


def run_baseline(llm, question: str, repo: str) -> list[dict]:
    out = raw_json(llm, BASELINE_SYSTEM, f"[질문]\n{question}\n\n[코드]\n{repo_code(repo)}")
    return [c for c in out.get("claims", []) if isinstance(c, dict) and "caller" in c and "callee" in c]


def summarize(rows: list[dict], arm: str) -> dict:
    sel = [r[arm] for r in rows if arm in r and r[arm] is not None]
    claims = sum(x["claims"] for x in sel)
    err = sum(x["hallucinated"] + x["wrong"] for x in sel)
    return {"n": len(sel), "claims": claims, "verified": sum(x["verified"] for x in sel),
            "hallucinated": sum(x["hallucinated"] for x in sel), "wrong": sum(x["wrong"] for x in sel),
            "unverifiable": sum(x["unverifiable"] for x in sel),
            "error_rate": round(err / claims, 3) if claims else None,
            "questions_with_error": sum(1 for x in sel if x["hallucinated"] + x["wrong"] > 0)}


def _check_ollama() -> None:
    """실험 시작 전에 Ollama가 떠 있고 모델이 받아져 있는지 확인한다(긴 실행 도중에 연결 오류로 죽지 않게)."""
    import httpx
    from dotenv import load_dotenv
    load_dotenv()
    base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat").rsplit("/api/", 1)[0]
    try:
        tags = httpx.get(base + "/api/tags", timeout=5).json()
    except Exception:
        raise SystemExit(f"Ollama에 연결할 수 없습니다({base}). Ollama 앱을 실행하거나 터미널에서 'ollama serve'를 먼저 띄우세요.")
    have = {m.get("name", "") for m in tags.get("models", [])}
    need = [os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"), os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b")]
    missing = [m for m in need if m not in have and m + ":latest" not in have]
    if missing:
        raise SystemExit(f"모델이 없습니다: {', '.join(missing)} — 'bash sh/setup_ollama.sh' 또는 'ollama pull {missing[0]}'")
    print(f"Ollama 확인: {base} · 모델 {', '.join(need)}")
    v = Path(__file__).resolve().parents[2] / "VERSION"
    print("Re-cat 버전:", v.read_text(encoding="utf-8").strip() if v.exists() else "unknown")


async def main(isolated: bool = False) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("ollama",), default="ollama")
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--questions", default="questions.json",
                    help="질문 목록 파일 이름(eval/ 밑) — 예: questions_unseen.json")
    ap.add_argument("--langsmith", action="store_true", help="결과를 LangSmith 데이터셋·실험으로 올린다(키 필요)")
    args = ap.parse_args()
    os.environ["LLM_BACKEND"] = args.backend
    _check_ollama()
    arms = set(args.arms.split(","))
    question_path = Path(args.questions)
    if not question_path.is_file():
        question_path = Path(__file__).parent / args.questions
    questions = json.loads(question_path.read_text(encoding="utf-8"))
    if args.limit:
        questions = questions[:args.limit]

    import server    # LLM·MCP·그래프 구성을 서버와 똑같이 쓴다
    from server import ChatRequest, _initial_state
    # CLI에서 격리한 등록부에만 unseen 저장소를 등록한다. 실제 등록부 쓰기는 거부.
    from backend.app.security import registry
    for repo in {q["repo"] for q in questions}:
        if repo not in server._POLICY.allowed_repos:
            if not isolated:
                raise RuntimeError("unseen evaluation requires the isolated CLI entry point")
            registry.register(server._POLICY, repo, sensitive=True)
    graphs: dict[str, object] = {}
    rows: list[dict] = []
    async with server.lifespan(server.app):
        deps = server.app.state.deps
        attempts_log: list[dict] = []
        orig = deps.quality_llm.explain

        async def logged(query, structure, snippets, attempt, feedback, on_token=None):
            out = await orig(query, structure, snippets, attempt, feedback, on_token)
            attempts_log.append({"attempt": attempt, "claims": out[0]["claims"]})
            return out
        deps.quality_llm.explain = logged

        for i, q in enumerate(questions, 1):
            g = graphs.setdefault(q["repo"], analyze_repo(q["repo"], ROOT / "var" / "data" / "repos" / q["repo"]))
            truth = ground_truth(g, q["entry"])
            row = {"repo": q["repo"], "entry": q["entry"], "question": q["question"],
                   "truth_pairs": len(truth["pairs"])}
            if "A" in arms:
                t0 = time.time()
                row["A"] = score(run_baseline(deps.quality_llm, q["question"], q["repo"]), truth)
                row["A"]["sec"] = round(time.time() - t0, 1)
            if arms & {"B", "C"}:
                attempts_log.clear()
                t0 = time.time()
                state = await server.app.state.graph.ainvoke(
                    _initial_state(ChatRequest(message=q["question"], thread_id=f"eval-{i}", repo=q["repo"]), []))
                sec = round(time.time() - t0, 1)
                row["entry_hit"] = state.get("entry_name") == q["entry"]
                row["entry_chosen"] = state.get("entry_name")
                row["route"], row["mode"] = state.get("route"), state.get("mode")     # B·C가 비면 원인 확인용
                cands = [c.get("qualname") for c in (state.get("candidates") or []) if isinstance(c, dict)]
                row["top3_candidates"] = cands[:3]
                row["top3_hit"] = q["entry"] in cands[:3]
                row["clarify"] = state.get("mode") == "clarify"
                # B·C는 Recat이 실제로 고른 진입점의 체인으로 채점한다 (진입점 검색 정확도는 entry_hit로 따로 본다)
                chosen = state.get("entry_name")
                truth_bc = ground_truth(g, chosen) if chosen else truth
                if attempts_log:
                    row["B"] = score(attempts_log[0]["claims"], truth_bc)
                    fallback = state.get("mode") == "no-llm-fallback"
                    row["C"] = None if fallback else score(attempts_log[-1]["claims"], truth_bc)
                    row["C_meta"] = {"attempts": state.get("attempts", 0), "fallback": fallback,
                                     "tool_calls": state.get("tool_calls", 0), "sec": sec}
                else:
                    row["B"] = row["C"] = None
                    row["C_meta"] = {"attempts": 0, "fallback": False, "note": "설명 단계에 도달하지 못함"}
            rows.append(row)
            print(f"[{i}/{len(questions)}] {q['repo']}:{q['entry']}  "
                  f"A={_fmt(row.get('A'))} B={_fmt(row.get('B'))} C={_fmt(row.get('C'))}"
                  f"  · 분류={row.get('route')} 시작={row.get('entry_chosen')}"
                  f"{' · 되묻기' if row.get('clarify') else ''}"
                  f"{' · top3적중' if row.get('top3_hit') and not row.get('entry_hit') else ''}", flush=True)

    # 분류 정확도: 전부 코드 질문으로 만든 세트이므로 route=="trace"가 아니면 분류가 틀린 것이다.
    traced = [r for r in rows if r.get("route") == "trace"]
    classification_rate = round(len(traced) / len(rows), 3) if rows else None
    result = {"backend": args.backend, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
              "models": {"fast": deps.fast_llm.name, "quality": deps.quality_llm.name},
              "question_set": args.questions,
              "questions": len(rows),
              "classification_rate": classification_rate,         # route==trace로 간 비율(전부 코드 질문)
              "entry_hit_rate": round(sum(1 for r in rows if r.get("entry_hit")) / len(rows), 3),
              "entry_hit_rate_traced": (round(sum(1 for r in traced if r.get("entry_hit")) / len(traced), 3)
                                       if traced else None),       # trace로 분류된 문항 기준
              "top3_hit_rate": round(sum(1 for r in rows if r.get("top3_hit")) / len(rows), 3),
              "clarify_rate": round(sum(1 for r in rows if r.get("clarify")) / len(rows), 3),
              "fallback_rate": round(sum(1 for r in rows if r.get("C_meta", {}).get("fallback")) / len(rows), 3),
              "avg_attempts": round(sum(r.get("C_meta", {}).get("attempts", 0) for r in rows) / len(rows), 2),
              "summary": {a: summarize(rows, a) for a in ("A", "B", "C")}, "rows": rows}
    qtag = Path(args.questions).stem.replace("questions", "").strip("_")   # 기본 questions.json이면 빈 문자열
    suffix = f"-{args.backend}-{qtag}" if qtag else f"-{args.backend}"    # 기존 파일명 규칙(ts-backend.json)을 유지
    out = Path(__file__).parent / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}{suffix}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(result)
    print(f"\n저장: {out.relative_to(ROOT)}")
    if args.langsmith:
        from tools.eval.langsmith_upload import UploadRefused, upload
        try:
            print("LangSmith 업로드:", upload(result))
        except UploadRefused as e:
            print("LangSmith 업로드 안 함:", e)


def _fmt(x: dict | None) -> str:
    return "-" if x is None else f"{x['hallucinated'] + x['wrong']}/{x['claims']}"


def print_report(r: dict) -> None:
    names = {"A": "A 코드만 준 LLM", "B": "B 구조 제공·검증 없음", "C": "C Recat 전체"}
    print(f"\n== 결과 ({r['backend']}, {r['models']['quality']}, 질문 {r['questions']}개) ==")
    print(f"{'방식':<22}{'주장수':>6}{'오류율':>8}{'오류 있는 질문':>14}{'판정불가':>8}")
    for a, label in names.items():
        s = r["summary"][a]
        er = "-" if s["error_rate"] is None else f"{s['error_rate']*100:.1f}%"
        print(f"{label:<22}{s['claims']:>6}{er:>8}{s['questions_with_error']:>10}/{s['n']:<3}{s['unverifiable']:>8}")
    traced_pct = "-" if r["entry_hit_rate_traced"] is None else f"{r['entry_hit_rate_traced']*100:.0f}%"
    print(f"분류 정확도(route=trace) {r['classification_rate']*100:.0f}% · "
          f"진입점 적중률 전체 {r['entry_hit_rate']*100:.0f}% · trace 기준 {traced_pct} · "
          f"top3 적중률 {r['top3_hit_rate']*100:.0f}% · 되묻기 비율 {r['clarify_rate']*100:.0f}% · "
          f"폴백 비율 {r['fallback_rate']*100:.0f}% · 평균 설명 시도 {r['avg_attempts']}")


async def isolated_cli() -> None:
    from dotenv import load_dotenv
    from backend.app.repositories.testing import isolated_database
    load_dotenv()
    with isolated_database():
        await main(isolated=True)


if __name__ == "__main__":
    asyncio.run(isolated_cli())
