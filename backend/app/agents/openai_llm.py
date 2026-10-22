"""LLM_BACKEND=openai — OpenAI 호환 엔드포인트 백엔드 (Base의 openai_llm 교체).

OPENAI_BASE_URL을 바꾸면 고객 환경의 호환 서버(vLLM 등)로도 쓸 수 있다.
보안 게이트가 "cloud"를 판정한 요청에만 이 백엔드가 쓰인다. 외부로 나가는 사용자 메시지·코드는
호출 전에 마스킹된 상태여야 한다. 실제 API로는 실행해 보지 못했고 모의 서버로만 확인했다.
"""
import asyncio
import json
import os

from langsmith import traceable

from .llm_common import (EXPLAIN_SCHEMA, EXPLAIN_SYSTEM, QUERY_EXPAND_SCHEMA, QUERY_EXPAND_SYSTEM,
                         ROUTE_SCHEMA, ROUTE_SYSTEM, SELECT_SCHEMA, SELECT_SYSTEM, explain_user_prompt,
                         DOC_SYSTEM, ROUTES, doc_user_prompt, post_json)


class _OpenAI:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = model
        self.usd_per_1k = usd_per_1k_tokens
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def endpoint(self) -> str:
        return self.base_url

    def _call(self, system: str, user: str, schema: dict | None, schema_name: str = "out"):
        body = {"model": self.name, "temperature": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "schema": schema, "strict": False}}
        data = post_json(f"{self.base_url}/chat/completions", body, timeout=120.0,
                         headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"})
        tokens = data.get("usage", {}).get("total_tokens", 0)
        return data["choices"][0]["message"]["content"], {
            "model": self.name, "tokens": tokens,
            "usd": round(tokens / 1000 * self.usd_per_1k, 6)}

    def _json(self, system: str, user: str, schema: dict, name: str):
        text, usage = self._call(system, user, schema, name)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError:
            return {}, usage


class OpenAIFastLLM(_OpenAI):
    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str, document_candidates: list[dict] | None = None) -> tuple[dict, dict]:
        candidates = document_candidates or []
        evidence = "\n".join(
            f"- {item['title']} ({item['file']}): {item.get('preview', '')}"
            for item in candidates[:3]
        ) or "(없음)"
        out, usage = self._json(ROUTE_SYSTEM, f"[질문]\n{query}\n\n[등록 문서 후보]\n{evidence}",
                                ROUTE_SCHEMA, "route")
        return {"route": out.get("route") if out.get("route") in ROUTES else "trace"}, usage

    @traceable(run_type="llm", name="fast_llm.select_expansions")
    def select_expansions(self, query: str, candidates: list[str]) -> tuple[list[str], dict]:
        out, usage = self._json(SELECT_SYSTEM, f"[질문]\n{query}\n[후보]\n" + "\n".join(candidates),
                                SELECT_SCHEMA, "select")
        chosen = [c for c in out.get("expand", []) if c in candidates]
        return (chosen or candidates), usage

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        text, usage = await asyncio.to_thread(self._call, "코드 호출 흐름을 설명하는 도우미입니다. 한 두 문장으로 답하세요.", query, None)
        if on_token is not None:
            on_token(text)
        return text, usage

    @traceable(run_type="llm", name="fast_llm.expand_query")
    def expand_query(self, query: str) -> tuple[list[str], dict]:
        """한국어 질문을 코드 검색용 영어 키워드로 바꾼다(진입점 검색 보강용). 실패·빈 응답이면 호출한 쪽이 기존 키워드 검색만 쓴다."""
        out, usage = self._json(QUERY_EXPAND_SYSTEM, query, QUERY_EXPAND_SCHEMA, "expand_query")
        kws = [k.strip() for k in out.get("keywords", []) if isinstance(k, str) and k.strip()]
        return kws[:6], usage


class OpenAIQualityLLM(_OpenAI):
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
                                EXPLAIN_SCHEMA, "explain")
        result = {"explanation": out.get("explanation", ""),
                  "claims": [c for c in out.get("claims", [])
                             if isinstance(c, dict) and "caller" in c and "callee" in c]}
        if on_token is not None and result["explanation"]:
            on_token(result["explanation"])
        return result, usage

    @traceable(run_type="llm", name="quality_llm.explain_doc")
    async def explain_doc(self, query: str, section: dict, check: dict) -> tuple[dict, dict]:
        out, usage = await asyncio.to_thread(self._json, DOC_SYSTEM, doc_user_prompt(query, section, check), EXPLAIN_SCHEMA, "explain_doc")
        return {"explanation": out.get("explanation", ""),
                "claims": [c for c in out.get("claims", [])
                           if isinstance(c, dict) and "caller" in c and "callee" in c]}, usage
