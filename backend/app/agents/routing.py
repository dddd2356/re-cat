"""모델과 독립적인 질문 분류 규칙. 모호한 chat 결과는 실제 fast 모델이 보완한다."""
import re


def route_by_rule(query: str) -> tuple[dict, dict]:
    if re.search(r"문서|명세|기획서|스펙|README|readme|\d+\s*단계", query):
        result = {"route": "doc"}
    else:
        trace = bool(re.search(
            r"호출|흐름|동작|구조|어떻게|무엇을|뭐하|설명|로직|함수|메서드|[A-Za-z]+\.[A-Za-z_]+|[A-Z][a-z]+[A-Z]", query))
        result = {"route": "trace" if trace else "chat"}
    return result, {"model": "rules", "tokens": (len(query) + len(str(result))) // 4 + 1, "usd": 0.0}
