"""감사 로그 — 누가 어느 저장소에 무엇을 요청했는지만 남기고 코드 본문은 남기지 않는다."""
from __future__ import annotations

import json
import time
from pathlib import Path

AUDIT_PATH = Path(__file__).resolve().parent.parent / "audit.log.jsonl"


def record(event: str, **meta) -> None:
    safe = {k: v for k, v in meta.items() if k not in {"source", "code", "snippet", "body"}}
    line = {"ts": round(time.time(), 3), "event": event, **safe}
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
