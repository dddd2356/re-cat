"""비밀 값 마스킹 — 모델·로그로 나가기 전 코드 조각에서 비밀 후보를 가린다.

정규식 기반이라 완전하지 않다(base64로 감싼 값, 문자열을 쪼개 이어 붙인 값은 놓친다).
그래서 '유출 0건'이 아니라 카나리 실험으로 잡은 건수와 놓친 건수를 보고하는 용도다.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("assignment", re.compile(
        r"(?i)((?:password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*)(['\"]?)([^\s'\";,]{4,})\2")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("kr_phone", re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")),
]


def mask(text: str) -> tuple[str, int]:
    """(마스킹된 텍스트, 가린 횟수)."""
    count = 0

    def repl_assign(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}***MASKED***{m.group(2)}"

    out = _PATTERNS[3][1].sub(repl_assign, text)
    for name, pat in _PATTERNS:
        if name == "assignment":
            continue
        out, n = pat.subn(f"***{name.upper()}***", out)
        count += n
    return out, count
