"""진입점 탐색 — 질문에서 '어느 함수에서 시작할지'를 찾는다 (RAG를 쓰는 유일한 지점).

호출 체인은 검색이 아니라 정적 분석이 정답이므로, 검색은 시작 함수를 고르는 데만 쓴다.
여기에는 키워드 검색(이름 직접 언급 + 토큰/한글 2-gram 겹침)이 구현돼 있다.
임베딩 검색(pgvector)은 같은 search() 인터페이스 뒤에 붙일 자리이며 이 프로토타입에는 없다.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[가-힣]+")


def _tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for w in _WORD.findall(text):
        if re.match(r"[A-Za-z]", w):
            for part in re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", w).replace("_", " ").split():
                toks.add(part.lower())
            toks.add(w.lower())
        else:
            toks.add(w)
            toks.update(w[i:i + 2] for i in range(len(w) - 1))
    return toks


def class_scope(query: str, functions: list[dict]) -> tuple[str, list[dict]] | None:
    """질문이 함수가 아니라 클래스(묶음)를 가리키는지 — 'OrderService 전체 흐름'처럼.
    클래스 이름은 언급했지만 그 클래스의 메서드(정규 이름·짧은 이름)는 언급하지 않았을 때만 클래스 범위로 본다.
    반환: (클래스 이름, 그 클래스의 메서드 목록 — 정의 순서) 또는 None."""
    owners: dict[str, list[dict]] = {}
    for f in functions:
        if "." in f["qualname"]:
            owners.setdefault(f["qualname"].rsplit(".", 1)[0], []).append(f)
    best = None
    for owner, methods in owners.items():
        short_owner = owner.split(".")[-1]
        if not re.search(rf"(?<![A-Za-z0-9_.]){re.escape(short_owner)}(?![A-Za-z0-9_]|\.[A-Za-z_])", query):
            continue
        named = any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(m['qualname'].rsplit('.', 1)[1])}(?![A-Za-z0-9_])", query)
                    for m in methods)
        if named:
            return None                       # 메서드를 짚었다 → 함수 하나에서 시작
        if best is None or len(owner) > len(best[0]):
            best = (owner, methods)
    return best


def search(query: str, functions: list[dict], top_k: int = 3,
          extra_terms: list[str] | None = None) -> list[dict]:
    """functions: list_functions 결과의 항목들. 점수 내림차순 상위 top_k를 반환한다.
    extra_terms: 질의 확장(fast LLM이 한국어 질문에서 뽑은 영어 키워드)으로 얻은 추가 토큰 — 기존 키워드
    점수에 그대로 더해질 뿐, 이름 일치(exact/short) 판정에는 영향을 주지 않는다(바꾸지 말 것 — 트러블슈팅 ⑨)."""
    q = _tokens(query)
    if extra_terms:
        q = q | {t.strip().lower() for t in extra_terms if t and t.strip()}
    scored = []
    for f in functions:
        name = f["qualname"]
        score = 0.0
        short = name.split(".")[-1]
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", query):
            score += 10          # 정규 이름을 그대로 언급 (질의 확장 전의 원문 query만 본다)
        elif re.search(rf"(?<![A-Za-z0-9_]){re.escape(short)}(?![A-Za-z0-9_])", query):
            score += 6           # 짧은 이름만 언급
        doc_toks = (_tokens(f.get("doc", "")) | _tokens(name) | _tokens(f.get("signature", ""))
                   | _tokens(f.get("file", "")))
        score += len(q & doc_toks) * 0.5
        if score > 0:
            scored.append({**f, "score": round(score, 2)})
    scored.sort(key=lambda x: (-x["score"], x["qualname"]))
    return scored[:top_k]
