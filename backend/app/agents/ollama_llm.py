"""LLM_BACKEND=ollama — 로컬 Ollama 백엔드 (Base의 ollama_llm 교체, 코드 호출 흐름 도메인).

fast(qwen3:0.6b)는 라우팅·탐색 선택·잡담, quality(qwen3:8b)는 설명과 claims 생성을 맡는다.
구조화 출력(format=JSON Schema)과 temperature=0을 쓴다. 비용은 로컬이라 0이며 토큰 수만 기록한다.
실제 qwen3:8b 평가 결과는 eval/results/에 보존한다. 모의 HTTP 어댑터 테스트와 실제 모델 평가는 구분한다.
"""
import asyncio
import json
import os

from langsmith import traceable

from .llm_common import (EXPLAIN_SCHEMA, EXPLAIN_SYSTEM, QUERY_EXPAND_SCHEMA, QUERY_EXPAND_SYSTEM,
                         ROUTE_SCHEMA, ROUTE_SYSTEM, SELECT_SCHEMA, SELECT_SYSTEM, explain_user_prompt,
                         DOC_SYSTEM, ROUTES, doc_user_prompt, post_json)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")


class _Ollama:
    def __init__(self, model: str, usd_per_1k_tokens: float = 0.0):
        self.name = model
        self.usd_per_1k = usd_per_1k_tokens

    def _call(self, system: str, user: str, schema: dict | None) -> tuple[str, dict]:
        body = {"model": self.name, "stream": False,
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if schema is not None:
            body["format"] = schema
        # qwen3 계열은 기본으로 '생각(thinking)' 토큰을 길게 낸다 — 느려지고 JSON 응답이 늦어진다.
        # 기본은 끄고(OLLAMA_THINK=1이면 켬), thinking을 지원하지 않는 모델에는 보내지 않는다.
        think = os.environ.get("OLLAMA_THINK", "")
        if think in ("0", "1"):
            body["think"] = think == "1"
        elif self.name.startswith("qwen3"):
            body["think"] = False
        data = post_json(OLLAMA_URL, body, timeout=float(os.environ.get("OLLAMA_TIMEOUT", "180")))
        tokens = data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
        return data["message"]["content"], {
            "model": self.name, "tokens": tokens,
            "usd": round(tokens / 1000 * self.usd_per_1k, 6)}

    def _json(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        text, usage = self._call(system, user, schema)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError:
            return {}, usage   # 호출한 쪽이 기본값으로 대체한다


class OllamaFastLLM(_Ollama):
    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str, document_candidates: list[dict] | None = None) -> tuple[dict, dict]:
        candidates = document_candidates or []
        evidence = "\n".join(
            f"- {item['title']} ({item['file']}): {item.get('preview', '')}"
            for item in candidates[:3]
        ) or "(없음)"
        out, usage = self._json(ROUTE_SYSTEM,
                                f"[질문]\n{query}\n\n[등록 문서 후보]\n{evidence}", ROUTE_SCHEMA)
        return {"route": out.get("route") if out.get("route") in ROUTES else "trace"}, usage

    @traceable(run_type="llm", name="fast_llm.select_expansions")
    def select_expansions(self, query: str, candidates: list[str]) -> tuple[list[str], dict]:
        out, usage = self._json(SELECT_SYSTEM, f"[질문]\n{query}\n[후보]\n" + "\n".join(candidates),
                                SELECT_SCHEMA)
        chosen = [c for c in out.get("expand", []) if c in candidates]
        return (chosen or candidates), usage    # 모델이 비우면 후보 전체(안전한 쪽)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        text, usage = await asyncio.to_thread(self._call, "코드 호출 흐름을 설명하는 도우미입니다. 한 두 문장으로 답하세요.", query, None)
        if on_token is not None:
            on_token(text)
        return text, usage

    @traceable(run_type="llm", name="fast_llm.expand_query")
    def expand_query(self, query: str) -> tuple[list[str], dict]:
        """한국어 질문을 코드 검색용 영어 키워드로 바꾼다(진입점 검색 보강용). 실패·빈 응답이면 호출한 쪽이 기존 키워드 검색만 쓴다."""
        out, usage = self._json(QUERY_EXPAND_SYSTEM, query, QUERY_EXPAND_SCHEMA)
        kws = [k.strip() for k in out.get("keywords", []) if isinstance(k, str) and k.strip()]
        return kws[:6], usage


class OllamaQualityLLM(_Ollama):
    @traceable(run_type="llm", name="quality_llm.explain")
    async def explain(self, query: str, structure: dict, snippets: dict[str, str],
                      attempt: int, feedback: str | None, on_token=None) -> tuple[dict, dict]:
        structure_text = "\n".join(f"{a} -> {b}" for a, b in structure["pairs"]) or "(호출 없음)"
        if structure.get("scope") == "class":   # 클래스 전체 질문: 메서드별로 나눠 설명하도록 범위를 알려 준다
            structure_text = (f"[범위] {structure['qualname']} 클래스 전체 — 메서드: {', '.join(structure['entries'])}\n"
                              + structure_text)
        out, usage = await asyncio.to_thread(self._json, EXPLAIN_SYSTEM,
                                explain_user_prompt(query, structure_text, snippets, feedback,
                                                    structure.get("procedure", "")),
                                EXPLAIN_SCHEMA)
        result = {"explanation": out.get("explanation", ""),
                  "claims": [c for c in out.get("claims", [])
                             if isinstance(c, dict) and "caller" in c and "callee" in c]}
        if on_token is not None and result["explanation"]:
            on_token(result["explanation"])
        return result, usage

    @traceable(run_type="llm", name="quality_llm.explain_doc")
    async def explain_doc(self, query: str, section: dict, check: dict) -> tuple[dict, dict]:
        out, usage = await asyncio.to_thread(self._json, DOC_SYSTEM, doc_user_prompt(query, section, check), EXPLAIN_SCHEMA)
        return {"explanation": out.get("explanation", ""),
                "claims": [c for c in out.get("claims", [])
                           if isinstance(c, dict) and "caller" in c and "callee" in c]}, usage
