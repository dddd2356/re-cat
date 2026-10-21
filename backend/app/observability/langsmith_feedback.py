"""LangSmith 피드백 — 검증 결과를 실행(run)마다 숫자 점수로 붙인다.

보내는 것은 정해진 키의 숫자뿐이다(코드·질문·답변 본문 없음). 그래서 클라우드 LangSmith여도
'입출력 숨김' 원칙과 충돌하지 않는다. 이 요청의 추적이 허용됐을 때(게이트 판정)만, 그리고
LangSmith 키가 있을 때만 보낸다. 실패해도 응답에는 영향을 주지 않는다.
"""
from __future__ import annotations

import os
from typing import Callable

FEEDBACK_KEYS = ("recat.verified", "recat.hallucinated", "recat.wrong", "recat.unverifiable",
                 "recat.caught_errors", "recat.explain_attempts", "recat.fallback", "recat.verified_ratio")


def scores_from(verdict: dict | None, attempts: int, mode: str | None, caught: int) -> dict[str, float]:
    v = verdict or {}
    ver, hal, wro, unv = (int(v.get(k, 0) or 0) for k in ("verified", "hallucinated", "wrong", "unverifiable"))
    total = ver + hal + wro + unv
    scores = {"recat.verified": ver, "recat.hallucinated": hal, "recat.wrong": wro,
              "recat.unverifiable": unv, "recat.caught_errors": int(caught or 0),
              "recat.explain_attempts": int(attempts or 0),
              "recat.fallback": 1 if mode in ("no-llm-fallback", "doc-no-llm") else 0}
    if total:
        scores["recat.verified_ratio"] = round(ver / total, 4)
    assert set(scores) <= set(FEEDBACK_KEYS)      # 정해진 숫자 키 외에는 보내지 않는다
    return {k: float(x) for k, x in scores.items()}


def has_key() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))


class FeedbackSender:
    def __init__(self, client_factory: Callable | None = None):
        self._factory = client_factory
        self._client = None
        self.sent = 0
        self.failed = 0

    def _get(self):
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                from langsmith import Client
                self._client = Client()
        return self._client

    def send(self, run_id: str, scores: dict[str, float]) -> int:
        n = 0
        try:
            client = self._get()
        except Exception:
            self.failed += len(scores)
            return 0
        for key, score in scores.items():
            try:
                client.create_feedback(run_id, key, score=score, comment=None)
                n += 1
            except Exception:
                self.failed += 1
        self.sent += n
        return n


def run_url(run_id: str | None) -> str | None:
    """관리자 화면 링크. LangSmith 화면 주소는 조직·프로젝트마다 달라서 템플릿을 환경변수로 받는다.
    예) LANGSMITH_RUN_URL=https://smith.langchain.com/o/ORG_ID/projects/p/PROJECT_ID/r/{run_id}"""
    tpl = os.environ.get("LANGSMITH_RUN_URL")
    return tpl.replace("{run_id}", run_id) if (tpl and run_id) else None
