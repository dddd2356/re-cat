"""모델과 독립적인 질문 분류 규칙. 모호한 chat 결과는 실제 fast 모델이 보완한다."""
import re


def route_by_rule(query: str) -> tuple[dict, dict]:
    # 문서의 대표 제목도 문서 질문 신호다. 예: "트러블슈팅했던 부분 설명해줘"는
    # '설명'이라는 단어만 보면 코드 추적으로 오해하기 쉽지만, 등록된 TROUBLESHOOTING.md를
    # 먼저 검색해야 한다.
    if re.search(r"문서|명세|기획서|스펙|README|readme|트러블\s*슈팅|troubleshooting|\d+\s*단계", query):
        result = {"route": "doc"}
    else:
        trace = bool(re.search(
            r"호출|흐름|동작|구조|어떻게|무엇을|뭐하|설명|로직|함수|메서드|[A-Za-z]+\.[A-Za-z_]+|[A-Z][a-z]+[A-Z]", query))
        result = {"route": "trace" if trace else "chat"}
    return result, {"model": "rules", "tokens": (len(query) + len(str(result))) // 4 + 1, "usd": 0.0}
