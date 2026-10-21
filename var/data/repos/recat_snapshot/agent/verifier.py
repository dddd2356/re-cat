"""결정적 검증 — LLM의 호출 관계 주장(claims)을 정적 분석 결과와 코드로 대조한다 (Base의 길이·번호 검증 교체).

LLM이 심판하지 않는다. 이 모듈이 확인하는 것은 '호출 관계의 사실 여부'뿐이며,
의도·이유 같은 해석의 정확성은 검증하지 못한다(그래서 화면에서 '해석(검증 안 됨)'으로 분리 표시).

판정:
  verified      주장한 (caller, callee) 쌍이 호출 체인에 있다
  hallucinated  callee나 caller가 저장소에 존재하지 않는 이름이다            → 오류
  wrong         두 이름은 실재하지만 체인에 그 호출이 없고, 미해석 호출과도 무관하다 → 오류
  unverifiable  체인에 없지만 caller에 해석 못 한 호출(동적·모호 등)이 있어 판정 불가 → 오류로 세지 않음
추가 검사(트러블슈팅 ⑤⑥)
  본문 검사   claims는 LLM이 스스로 신고한 목록이라, 본문에만 적고 claims에서 빼면 빠져나간다.
              본문에 'Class.method' 꼴이나 `백틱` 으로 적힌 이름이 저장소에 없으면 hallucinated로 센다.
  빈 설명     설명이 비었거나, 호출 구조가 있는데 주장이 하나도 없으면 통과로 치지 않는다
              (JSON이 깨져 빈 결과가 오면 '오류 0개 = 통과'로 집계되던 문제).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 본문 속 코드 이름: `백틱` 또는 점으로 이은 식별자(A.b). 한글 조사가 바로 붙어도 잘리도록 영문·숫자·_만 식별자로 본다.
_TICK = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
_DOTTED = re.compile(r"(?<![A-Za-z0-9_./])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
_FILE_EXT = {"py", "java", "kt", "js", "ts", "md", "txt", "rst", "json", "yaml", "yml", "xml", "html", "sql", "gradle"}
_SKIP_HEAD = {"self", "this", "super", "cls", "os", "sys", "re", "json", "e", "i", "eg", "ie", "vs", "etc"}


@dataclass
class Verdict:
    verified: list[dict] = field(default_factory=list)
    hallucinated: list[dict] = field(default_factory=list)
    wrong: list[dict] = field(default_factory=list)
    unverifiable: list[dict] = field(default_factory=list)
    empty: bool = False            # 설명이 비었거나, 호출이 있는데 주장이 없음

    @property
    def errors(self) -> list[dict]:
        return self.hallucinated + self.wrong

    @property
    def ok(self) -> bool:
        return not self.errors and not self.empty

    def feedback(self) -> str:
        """재설명 요청에 붙일 피드백 — 틀린 주장만 구체적으로 알려준다."""
        lines = ["다음 호출 관계 주장은 코드 분석 결과와 다릅니다. 해당 주장을 빼거나 고쳐 다시 설명하세요:"]
        if self.empty:
            lines.append("- 설명이 비어 있거나 호출 관계 주장(claims)이 없습니다. 호출 구조의 관계를 claims에 빠짐없이 담으세요")
        for c in self.hallucinated:
            if c["caller"] == "(설명 본문)":
                lines.append(f"- 설명 본문의 {c['callee']}: 저장소에 없는 이름입니다. 지우거나 정확한 함수 이름(qualname)으로 쓰세요")
            else:
                lines.append(f"- {c['caller']} → {c['callee']}: 저장소에 없는 이름이 포함되어 있습니다")
        for c in self.wrong:
            lines.append(f"- {c['caller']} → {c['callee']}: 호출 체인에 없는 관계입니다")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "verified": len(self.verified),
                "hallucinated": len(self.hallucinated), "wrong": len(self.wrong),
                "unverifiable": len(self.unverifiable), "empty": self.empty,
                "errors": self.errors, "unverifiable_claims": self.unverifiable}


def _short(name: str) -> str:
    return name.split(".")[-1]


def names_in_text(text: str) -> list[str]:
    """설명 본문에서 코드 이름으로 보이는 것만 뽑는다(파일명·self.x 같은 것은 제외)."""
    out: list[str] = []
    for n in _TICK.findall(text or "") + _DOTTED.findall(text or ""):
        parts = n.split(".")
        if parts[-1].lower() in _FILE_EXT or parts[0].lower() in _SKIP_HEAD or n in out:
            continue
        out.append(n)
    return out


def _known_name(name: str, known: set[str], known_short: set[str]) -> bool:
    if name in known or any(k.endswith("." + name) or name.endswith("." + k) for k in known):
        return True
    parts = name.split(".")
    if len(parts) == 1:
        return name in known_short
    # 'stock.decrease'처럼 소문자로 시작하면 인스턴스 변수 경유 호출 — 메서드 이름만 확인한다.
    # 'AuditLogger.write'처럼 대문자로 시작하면 클래스.메서드로 보고 위에서 이미 못 찾았으므로 없는 이름.
    return parts[0][:1].islower() and parts[-1] in known_short


def verify(claims: list[dict], chain: dict, text: str | None = None, require_claims: bool = False) -> Verdict:
    """chain은 MCP get_chain의 결과(pairs, unresolved, known_symbols).
    text를 주면 설명 본문 속 코드 이름도 검사하고, require_claims면 빈 주장을 통과시키지 않는다."""
    pairs = {tuple(p) for p in chain["pairs"]}
    known = set(chain["known_symbols"])
    known_short = {_short(k) for k in known}
    unresolved = chain["unresolved"]
    v = Verdict()
    seen: set[tuple[str, str]] = set()
    for c in claims:
        key = (c.get("caller", ""), c.get("callee", ""))
        if key in seen:
            continue
        seen.add(key)
        caller, callee = key
        item = {"caller": caller, "callee": callee}
        if key in pairs:
            v.verified.append(item)
            continue
        caller_known = caller in known or caller in known_short
        callee_known = callee in known or callee in known_short
        if not caller_known or not callee_known:
            v.hallucinated.append(item)
            continue
        # 체인에 없다 — 해석 못 한 호출이 같은 호출자·같은 메서드 이름이면 판정 불가
        overlap = any(u["caller"] in (caller, _short(caller)) and
                      _short(u["text"]) in (_short(callee), callee)
                      for u in unresolved)
        (v.unverifiable if overlap else v.wrong).append(item)
    claimed = {x for c in claims for x in (c.get("caller", ""), c.get("callee", ""))}
    for n in names_in_text(text):
        if n not in claimed and not _known_name(n, known, known_short):
            v.hallucinated.append({"caller": "(설명 본문)", "callee": n})
    if (text is not None and not text.strip()) or (require_claims and not claims):
        v.empty = True
    return v
