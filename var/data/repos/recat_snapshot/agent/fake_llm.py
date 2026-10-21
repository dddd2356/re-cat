"""결정론적 Fake LLM — 네트워크 없이 파이프라인 전체를 재현한다 (Base의 fake_llm 교체).

일부러 '첫 설명에 틀린 호출 관계를 하나 섞는' 동작을 넣어(FAKE_FLAWED_FIRST=1, 기본),
검증 노드가 잡아내고 재설명으로 고쳐지는 경로가 실제로 실행되는지 확인할 수 있다.
"""
import os
import re

from langsmith import traceable


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


class FastLLM(FakeLLM):
    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        if re.search(r"문서|명세|기획서|스펙|README|readme|\d+\s*단계", query):
            result = {"route": "doc"}
            return result, self._usage(query, str(result))
        trace = bool(re.search(
            r"호출|흐름|동작|구조|어떻게|무엇을|뭐하|설명|로직|함수|메서드|주문|결제|재고|[A-Za-z]+\.[A-Za-z_]+|[A-Z][a-z]+[A-Z]", query))
        result = {"route": "trace" if trace else "chat"}
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.select_expansions")
    def select_expansions(self, query: str, candidates: list[str]) -> tuple[list[str], dict]:
        """탐색 노드가 다음에 열 함수를 고른다. fake는 후보 전체를 고른다(로깅류만 제외)."""
        chosen = [c for c in candidates if not re.search(r"log|print", c, re.I)]
        return chosen, self._usage(query + str(candidates), str(chosen))

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        reply = "안녕하세요! 코드의 호출 흐름이 궁금한 함수나 기능을 물어보세요."
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


def _n(q: str) -> str:
    """같은 클래스 안에서는 짧은 이름으로 읽히게 한다."""
    return q.split(".")[-1] if "." in q else q


def _doc_line(src: str) -> str:
    m = re.search(r'"""\s*(.+?)\s*(?:"""|\n)', src or "") or re.search(r"/\*\*\s*\*?\s*(.+?)\s*(?:\*/|\n)", src or "")
    return m.group(1).rstrip(".") if m else ""


def _gist(text: str, limit: int = 320) -> str:
    """문서 절의 앞부분을 평문으로(코드 블록·표·마크다운 기호 제거). 실제 LLM이면 모델이 요약한다."""
    out, in_code = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        t = line.strip()
        if in_code or not t or t.startswith("|") or t.startswith(">"):
            continue
        t = re.sub(r"\*\*|`", "", t).lstrip("-*0123456789. ").strip()
        if t:
            out.append(t)
        if sum(len(x) for x in out) > limit:
            break
    s = " ".join(out)
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def _join(names: list[str]) -> str:
    return ", ".join(names[:-1]) + (" 그리고 " if len(names) > 1 else "") + names[-1] if names else ""


def _conversational(structure: dict, pairs: list[tuple[str, str]], snippets: dict[str, str]) -> list[str]:
    """정적 분석 결과를 대화체로 풀어 쓴다(LLM 없음). 실제 LLM이 붙으면 이 자리를 모델 설명이 대신한다."""
    by: dict[str, list[str]] = {}
    for a, b in pairs:
        by.setdefault(a, []).append(b)
    entries = structure.get("entries") or [structure["qualname"]]
    unres = {u["caller"]: u for u in structure.get("unresolved", [])}
    out: list[str] = []
    if structure.get("scope") == "class":
        busy = [m for m in entries if by.get(m)]
        out.append(f"{structure['qualname']}에는 메서드가 {len(entries)}개 있고, 그중 {len(busy)}개가 다른 함수를 호출해요."
                   + (f" 흐름의 중심은 가장 많이 호출하는 {_n(max(busy, key=lambda m: len(by[m])))}입니다." if busy else ""))
    else:
        out.append(f"{structure['qualname']}에서 시작하는 호출은 {len(pairs)}개이고, 깊이 {structure.get('depth', 3)}까지 따라갔어요."
                   if pairs else f"{structure['qualname']}는 정적 분석으로 확인되는 다른 함수 호출이 없어요.")
    out.append("")
    for m in entries:
        doc = _doc_line(snippets.get(m, ""))
        head = f"• {_n(m)}" + (f" — {doc}" if doc else "")
        if by.get(m):
            out.append(f"{head}\n  {_join([_n(x) if x.rsplit('.', 1)[0] == m.rsplit('.', 1)[0] else x for x in by[m]])}를 호출해요.")
        elif m in unres:
            u = unres[m]
            out.append(f"{head}\n  {u['text']}로 이름을 조합해 호출해서, 실제로 어떤 함수가 불리는지는 정적 분석으로 확인할 수 없어요. 코드를 직접 확인해 주세요.")
        else:
            out.append(f"{head}\n  다른 함수를 호출하지 않아요.")
    deeper = [a for a in by if a not in entries]
    if deeper:
        out.append("")
        out.append("그 아래로는 " + "; ".join(f"{a}가 {_join([_n(x) for x in by[a]])}를" for a in deeper[:4])
                   + (" 호출하고" if len(deeper) > 4 else " 호출해요.")
                   + (f", 그 밖에 {len(deeper) - 4}개 함수가 더 이어져요." if len(deeper) > 4 else ""))
    return out


class QualityLLM(FakeLLM):
    @traceable(run_type="llm", name="quality_llm.explain")
    async def explain(self, query: str, structure: dict, snippets: dict[str, str],
                      attempt: int, feedback: str | None, on_token=None) -> tuple[dict, dict]:
        pairs = [tuple(p) for p in structure["pairs"]]
        claims = [{"caller": a, "callee": b} for a, b in pairs]
        lines = _conversational(structure, pairs, snippets)
        if attempt == 1 and os.environ.get("FAKE_FLAWED_FIRST", "1") == "1" and pairs:
            # 의도된 결함: 존재하지 않는 호출을 하나 섞는다 → 검증 노드가 잡아야 한다
            claims.append({"caller": pairs[0][0], "callee": "AuditLogger.write"})
            lines.append(f"참고로 {_n(pairs[0][0])}는 AuditLogger.write로 감사 로그도 남깁니다.")
        explanation = "\n".join(lines)
        if on_token is not None:
            on_token(explanation)
        return {"explanation": explanation, "claims": claims}, self._usage(query + str(feedback), explanation)

    @traceable(run_type="llm", name="quality_llm.explain_doc")
    async def explain_doc(self, query: str, section: dict, check: dict) -> tuple[dict, dict]:
        """대조 결과를 그대로 풀어 쓴다. 대응 판단은 연결 근거에 따라 '보입니다/근거를 찾지 못했습니다'로만 말한다."""
        c = check["counts"]
        lines = [f"문서 '{section['title']}' 절({section['file']})에 따르면:"]
        gist = _gist(section.get("text", ""))
        if gist:
            lines.append(gist)
        rel = check.get("relation")
        if rel:
            msg = {"direct": f"질문한 {rel['symbol']}은(는) 이 절에 이름이 직접 적혀 있어, 이 단계에 해당하는 코드로 보입니다.",
                   "linked": f"질문한 {rel['symbol']}은(는) 절에 적힌 함수와 호출로 이어져 있어, 이 단계와 관련된 코드로 보입니다.",
                   "none": f"질문한 {rel['symbol']}과(와) 이 절을 잇는 코드상 근거는 찾지 못했습니다. 문서가 다른 이름을 쓰고 있을 수 있습니다."}
            lines.append(msg[rel["relation"]])
        total = sum(c.values())
        if total:
            lines.append(f"문서에 적힌 호출 관계 {total}개 중 일치 {c['match'] + c['indirect']}개, 불일치 {c['mismatch']}개, "
                         f"코드에 없는 이름 {c['missing']}개, 확인 안 됨 {c['unknown']}개입니다.")
        if c["mismatch"] or c["missing"]:
            lines.append("문서가 코드보다 오래되었을 수 있으니, 불일치 항목은 담당자 확인이 필요합니다.")
        claims = [{"caller": x["caller"], "callee": x["callee"]} for x in check["claims"] if x["verdict"] == "match"]
        for x in claims:
            lines.append(f"- {x['caller']} 가 {x['callee']} 를 호출합니다.")
        explanation = "\n".join(lines)
        return {"explanation": explanation, "claims": claims}, self._usage(query, explanation)
