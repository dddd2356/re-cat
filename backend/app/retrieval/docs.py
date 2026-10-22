"""문서 근거 — 저장소 안의 문서(기획서·명세·README)를 절 단위로 나누고, 코드 구조와 연결한다.

역할 분담
  정적 분석  무엇이 무엇을 호출하는가 (확정)
  이 모듈    문서가 무엇이라고 말하는가 + 그 말이 코드와 맞는가 (대조는 결정적, LLM 없음)
  LLM        둘을 읽기 좋게 설명 (대응 판단은 '추정'으로 표시)

문서는 정답이 아니다. 오래된 저장소일수록 문서가 낡아 있으므로, 문서 내용은 '문서 근거'로만 보여 주고
문서에 적힌 호출 관계는 호출 그래프와 대조해 아래처럼 판정한다.
  match       일치 — 직접 호출이 있다
  indirect    일치(간접) — 깊이 제한 안에서 경로로 이어진다
  mismatch    불일치 — 두 이름은 코드에 있지만 호출 경로가 없고, 해석 못 한 호출도 없다
  missing     코드에 없는 이름 — 문서가 가리키는 함수가 저장소에 없다(낡은 문서 후보)
  unknown     확인 안 됨 — 해석 못 한 호출(동적·모호)이 있어 없다고 단정할 수 없다, 또는 이름이 여러 개

검색은 키워드(이름 직접 언급, 토큰·한글 2-gram 겹침, 'N단계' 번호 일치)다. 임베딩 검색은 같은
search_sections() 인터페이스 뒤에 붙일 자리이며 이 프로토타입에는 없다.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.analyzers.base import CallGraph

from .entrypoint_search import _tokens

DOC_EXT = {".md", ".markdown", ".txt", ".rst"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "target"}
MAX_FILE_BYTES = 200_000
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_STEP = re.compile(r"(\d+)\s*단계")
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
# 문서 속 호출 관계 표기: `A` → `B`, A -> B, `A`가 `B`를 호출
_ARROW = re.compile(rf"`?({_IDENT})`?\s*(?:→|->)\s*`?({_IDENT})`?")
_KO_CALL = re.compile(rf"`({_IDENT})`\s*(?:이|가|은|는|에서)\s*`({_IDENT})`\s*(?:을|를)?\s*(?:먼저\s*)?호출")


@dataclass
class Section:
    id: str            # "<파일>#<번호>"
    file: str
    title: str         # 상위 제목 > 제목
    line: int
    text: str
    claims: list[tuple[str, str]] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)


def _doc_files(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if p.is_symlink() or not p.is_file() or p.suffix.lower() not in DOC_EXT:
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        yield p, rel.as_posix()


def extract_claims(text: str) -> list[tuple[str, str]]:
    """문서 문장에서 'A가 B를 호출한다' 형태의 주장을 규칙으로 뽑는다(LLM 없음).
    코드 이름은 백틱이나 화살표로 적힌 것만 잡는다 — 자연어 표현('결제 모듈')은 잡지 못한다."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        for rx in (_ARROW, _KO_CALL):
            for a, b in rx.findall(line):
                if (a, b) not in out and a != b:
                    out.append((a, b))
    return out


def _mentions(text: str) -> list[str]:
    names = re.findall(rf"`({_IDENT})`", text)
    for a, b in extract_claims(text):
        names += [a, b]
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def load_sections(root: Path, extra: Path | None = None) -> list[Section]:
    """root(저장소) 안 문서 + extra(관리자가 따로 올린 문서, 'uploaded/'로 표시)를 절로 나눈다."""
    sections: list[Section] = []
    files = list(_doc_files(root))
    if extra is not None and extra.is_dir():
        files += [(p, "uploaded/" + rel) for p, rel in _doc_files(extra)]
    for path, rel in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        stack: list[tuple[int, str]] = []
        cur_title, cur_line, buf = Path(rel).name, 1, []

        def flush():
            body = "\n".join(buf).strip()
            if body or cur_title:
                text = body
                sections.append(Section(id=f"{rel}#{len([s for s in sections if s.file == rel]) + 1}",
                                        file=rel, title=cur_title, line=cur_line, text=text,
                                        claims=extract_claims(text), mentions=_mentions(text)))

        for i, raw in enumerate(lines, 1):
            m = _HEADING.match(raw)
            if not m:
                buf.append(raw)
                continue
            if buf and "\n".join(buf).strip():
                flush()
            level, title = len(m.group(1)), m.group(2)
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
            cur_title = " > ".join(t for _, t in stack[1:]) if len(stack) > 1 else title
            cur_line, buf = i, []
        if buf and "\n".join(buf).strip():
            flush()
    # 긴 절도 빠짐없이 임베딩/근거로 쓸 수 있게 겹치는 청크로 분리한다.
    chunks = []
    for sec in sections:
        if len(sec.text) <= 4000:
            chunks.append(sec)
            continue
        for i,start in enumerate(range(0,len(sec.text),3600)):
            body = sec.text[start:start+4000]
            chunks.append(Section(id=sec.id if i==0 else f"{sec.id}:chunk{i+1}", file=sec.file,
                                  title=sec.title, line=sec.line+sec.text[:start].count("\n"), text=body,
                                  claims=extract_claims(body), mentions=_mentions(body)))
    return chunks


def search_sections(query: str, sections: list[Section], top_k: int = 3) -> list[dict]:
    q = _tokens(query)
    steps = set(_STEP.findall(query))
    scored = []
    for s in sections:
        score = len(q & _tokens(s.title)) * 2.0 + len(q & _tokens(s.text)) * 0.5
        if steps and steps & set(_STEP.findall(s.title)):
            score += 6                                      # '3단계'처럼 번호를 짚은 질문
        for name in s.mentions:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", query):
                score += 3                                  # 질문이 이 절에 적힌 코드 이름을 언급
        if score > 0:
            scored.append({"id": s.id, "file": s.file, "title": s.title, "line": s.line,
                           "score": round(score, 2), "preview": s.text[:120]})
    scored.sort(key=lambda x: (-x["score"], x["id"]))
    return scored[:top_k]


# ---- 코드와의 대조 (결정적) ----

def _lookup(g: CallGraph, name: str) -> tuple[str, list[str]]:
    """문서의 이름 → ('found'|'missing'|'ambiguous', [qualname...]).
    LangGraph 노드는 `g.add_node("explain", explain_node)`처럼 짧은 키로 등록되지만 실제
    함수는 `explain_node`처럼 접미사가 붙는다 — 그 접미사 붙은 이름이 유일하면 우선한다
    (그래프 다이어그램의 "explain"이 무관한 동명 메서드와 겹쳐 모호해지는 것을 막는다)."""
    exact = sorted({s.qualname for s in g.symbols.values() if s.qualname == name})
    if exact:
        return "found", exact
    if "." in name:
        return "missing", []
    by_node = sorted({s.qualname for s in g.symbols.values() if s.name == f"{name}_node"})
    if len(by_node) == 1:
        return "found", by_node
    by_short = sorted({s.qualname for s in g.symbols.values() if s.name == name})
    if len(by_short) == 1:
        return "found", by_short
    if len(by_short) > 1:
        return "ambiguous", by_short
    return "missing", []


def _sid(g: CallGraph, qualname: str) -> str:
    return next(s.id for s in g.symbols.values() if s.qualname == qualname)


def _path(g: CallGraph, src: str, dst: str, depth: int) -> list[str] | None:
    """src에서 dst까지 해석된 호출만 따라간 최단 경로(qualname 목록). 깊이 제한."""
    start, goal = _sid(g, src), _sid(g, dst)
    out: dict[str, list[str]] = {}
    for e in g.edges:
        if e.resolved:
            out.setdefault(e.caller, []).append(e.callee)
    q, prev = deque([(start, 0)]), {start: None}
    while q:
        cur, d = q.popleft()
        if cur == goal:
            path, x = [], cur
            while x is not None:
                path.append(g.symbols[x].qualname)
                x = prev[x]
            return path[::-1]
        if d >= depth:
            continue
        for n in out.get(cur, []):
            if n not in prev:
                prev[n] = cur
                q.append((n, d + 1))
    return None


def _has_unresolved(g: CallGraph, qualname: str, depth: int) -> list[dict]:
    """qualname에서 깊이 안에 도달하는 함수들 중 해석 못 한 호출(외부 호출 제외)."""
    sid = _sid(g, qualname)
    return [u for u in g.unresolved_texts(sid, depth) if u.get("reason") != "external"]


def check_claim(g: CallGraph, a: str, b: str, depth: int = 3) -> dict:
    res = {"doc": f"{a} → {b}", "caller": a, "callee": b}
    sa, qa = _lookup(g, a)
    sb, qb = _lookup(g, b)
    missing = [n for n, s in ((a, sa), (b, sb)) if s == "missing"]
    if missing:
        return {**res, "verdict": "missing", "detail": "코드에 없는 이름: " + ", ".join(missing)}
    if "ambiguous" in (sa, sb):
        cands = qa if sa == "ambiguous" else qb
        return {**res, "verdict": "unknown", "detail": "같은 이름이 여러 개: " + ", ".join(cands)}
    ca, cb = qa[0], qb[0]
    res.update(caller=ca, callee=cb)
    p = _path(g, ca, cb, depth)
    if p and len(p) == 2:
        return {**res, "verdict": "match", "detail": "직접 호출", "path": p}
    if p:
        return {**res, "verdict": "indirect", "detail": "경로: " + " → ".join(p), "path": p}
    unres = _has_unresolved(g, ca, depth)
    if unres:
        u = unres[0]
        return {**res, "verdict": "unknown",
                "detail": f"해석 못 한 호출이 있어 단정 불가 ({g.symbols[u['caller']].qualname}: {u['text']}, {u['reason']})"}
    return {**res, "verdict": "mismatch", "detail": f"코드에서 {ca}는 {cb}를 호출하지 않음(깊이 {depth} 이내)"}


def check_section(g: CallGraph, section: Section, depth: int = 3) -> dict:
    claims = [check_claim(g, a, b, depth) for a, b in section.claims]
    names = []
    for n in section.mentions:
        status, q = _lookup(g, n)
        names.append({"name": n, "status": status, "qualnames": q})
    counts = {k: sum(1 for c in claims if c["verdict"] == k)
              for k in ("match", "indirect", "mismatch", "missing", "unknown")}
    return {"section": section.id, "title": section.title, "claims": claims,
            "names": names, "counts": counts}


def relate_symbol(g: CallGraph, qualname: str, check: dict, depth: int = 3) -> dict:
    """질문한 함수가 이 절과 어떻게 이어지는가 — 결정적으로 확인되는 근거만 말한다.
    'direct'  절에 이름이 직접 적혀 있다
    'linked'  절에 적힌 함수에서 호출로 이어지거나, 그 함수를 호출한다
    'none'    코드상 연결 근거가 없다(대응이 틀렸다는 뜻은 아니다 — 문서가 이름을 안 적었을 수 있다)"""
    named = [q for n in check["names"] if n["status"] == "found" for q in n["qualnames"]]
    if qualname in named:
        return {"symbol": qualname, "relation": "direct", "detail": "문서 절에 이름이 직접 적혀 있음"}
    for q in named:
        p = _path(g, q, qualname, depth) or _path(g, qualname, q, depth)
        if p:
            return {"symbol": qualname, "relation": "linked", "detail": "호출 경로: " + " → ".join(p)}
    return {"symbol": qualname, "relation": "none",
            "detail": "절에 적힌 이름과 호출로 이어지지 않음" if named else "절에 코드 이름이 적혀 있지 않음"}
