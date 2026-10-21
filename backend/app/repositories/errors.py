"""저장소 계층의 도메인 오류. FastAPI에 의존하지 않는다."""
from __future__ import annotations


class DeletionError(RuntimeError):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)
