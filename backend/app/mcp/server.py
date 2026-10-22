"""Recat MCP 서버 — 정적 분석 결과를 읽기 전용 도구로 노출한다 (Base의 mcp_server.py 교체).

별도 subprocess(stdio)로 기동되며, 도구는 모두 읽기 전용이다.
  Tool     list_functions / get_callees / get_chain / get_overview / get_group / get_callers / get_source / get_file   (질의형 — 인자마다 결과가 다르다)
           search_docs / get_doc_section / check_doc_section   (문서 근거 — 대조는 결정적)
           get_status / reanalyze   (관리자 화면용 — reanalyze는 분석 캐시만 갱신하고 저장소 파일은 바꾸지 않는다)
  Resource repo://{repo}/symbols                                     (인덱스된 함수 목록, 정적)
  Prompt   code_trace_prompt                                         (호출 흐름 설명 표준 절차)
허용 목록 밖 저장소는 거부하고, get_source는 경로 이탈을 막고 길이를 제한한 뒤 마스킹한다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastmcp import FastMCP

from backend.app.analyzers.base import CallGraph
from backend.app.analyzers.loader import analyze_repo
from backend.app.retrieval import docs as docmod
from backend.app.security.masking import mask
from backend.app.security.path_guard import docs_dir, repo_root, safe_file
from backend.app.security.policy import load_policy
from backend.app.security import registry

mcp = FastMCP(name="recat-code-trace")
POLICY = load_policy()
MAX_CACHED_REPOS = int(os.environ.get("RECAT_MCP_CACHE_SIZE", "20"))   # 저장소가 늘어나도 메모리가 무한히 안 커지게
_cache: "OrderedDict[str, CallGraph]" = OrderedDict()                  # 접근 순서 = LRU 순서
_analyzed_at: dict[str, float] = {}
_docs: dict[str, list] = OrderedDict()
_revisions: dict[str, int] = {}


def _evict_lru() -> None:
    """한도를 넘으면 가장 오래 안 쓴 저장소의 분석 캐시·문서 색인을 같이 내린다(다시 물으면 재분석)."""
    while len(_cache) > MAX_CACHED_REPOS:
        oldest, _ = _cache.popitem(last=False)
        _analyzed_at.pop(oldest, None)
        _docs.pop(oldest, None)
        _revisions.pop(oldest, None)


def _sync_revision(repo):
    revision = registry.revision(repo)
    if _revisions.get(repo) != revision:
        _cache.pop(repo,None)
        _docs.pop(repo,None)
        _analyzed_at.pop(repo,None)
        _revisions[repo] = revision


def _graph(repo: str) -> CallGraph:
    registry.apply(POLICY)                            # 관리자가 등록한 저장소 반영(합집합)
    root = repo_root(repo, POLICY.allowed_repos)      # 허용 목록·경로 이탈 검사
    _sync_revision(repo)
    if repo in _cache:
        _cache.move_to_end(repo)                      # 최근 사용으로 갱신
        return _cache[repo]
    _cache[repo] = analyze_repo(repo, root)
    _analyzed_at[repo] = round(time.time(), 3)
    _evict_lru()
    return _cache[repo]


def _sections(repo: str) -> list:
    """저장소 안 문서(md·txt·rst)의 절 목록. 분석 캐시와 같이 갱신된다."""
    registry.apply(POLICY)
    root = repo_root(repo, POLICY.allowed_repos)
    _sync_revision(repo)
    if repo not in _docs:
        _docs[repo] = docmod.load_sections(root, docs_dir(repo, POLICY.allowed_repos))
    if isinstance(_docs, OrderedDict):
        _docs.move_to_end(repo)
        while len(_docs) > MAX_CACHED_REPOS:
            oldest, _ = _docs.popitem(last=False)
            # 문서만 조회한 저장소도 무한히 revision 표를 남기지 않는다.
            if oldest not in _cache:
                _revisions.pop(oldest, None)
    return _docs[repo]


def _section(repo: str, section_id: str):
    for sec in _sections(repo):
        if sec.id == section_id:
            return sec
    raise ValueError(f"section not found: {section_id}")


def _stats(repo: str, g: CallGraph) -> dict:
    ov = g.overview()
    secs = _sections(repo)
    return {"repo": repo, "function_count": len(g.symbols), "edge_count": len(g.edges),
            "unresolved": sum(n["unresolved"] for n in ov["nodes"]),
            "doc_files": len({x.file for x in secs}), "doc_sections": len(secs),
            "analyzed_at": _analyzed_at.get(repo)}


def _resolve(g: CallGraph, symbol: str):
    found = g.find(symbol)
    if not found:
        raise ValueError(f"symbol not found: {symbol}")
    if len(found) > 1:
        raise ValueError(f"ambiguous symbol {symbol!r}: " + ", ".join(s.id for s in found))
    return found[0]


@mcp.tool()
def list_functions(repo: str, limit: int = 500) -> dict:
    """저장소의 함수·메서드 목록(식별자, 이름, 시그니처, 한 줄 설명, 파일 경로)을 반환한다."""
    g = _graph(repo)
    items = [{"id": s.id, "qualname": s.qualname, "language": s.language,
              "signature": s.signature, "doc": s.doc.splitlines()[0] if s.doc else "", "file": s.file}
             for s in g.symbols.values()]
    return {"repo": repo, "count": len(items), "functions": items[:limit]}


@mcp.tool()
def get_callees(repo: str, symbol: str) -> dict:
    """함수가 직접 호출하는 대상을 반환한다. 해석하지 못한 호출은 unresolved에 이유와 함께 남는다."""
    g = _graph(repo)
    s = _resolve(g, symbol)
    callees, unresolved = [], []
    for e in g.callees(s.id):
        if e.resolved:
            callee = g.symbols[e.callee]
            callees.append({"id": callee.id, "qualname": callee.qualname, "file": callee.file,
                            "text": e.callee_text, "line": e.line})
        else:
            item = {"text": e.callee_text, "line": e.line, "reason": e.reason}
            if e.candidates:
                item["candidates"] = [g.symbols[c].qualname for c in e.candidates]
            unresolved.append(item)
    return {"symbol": s.id, "qualname": s.qualname, "callees": callees, "unresolved": unresolved}


@mcp.tool()
def get_chain(repo: str, symbol: str, depth: int = 3) -> dict:
    """symbol에서 시작하는 호출 체인(깊이 제한)을 쌍 목록과 미해석 목록으로 반환한다.
    검증 노드가 LLM의 주장을 대조할 때 쓰는 정답 집합이다."""
    g = _graph(repo)
    s = _resolve(g, symbol)
    depth = max(1, min(depth, 6))
    q = lambda sid: g.symbols[sid].qualname
    pairs = sorted((q(a), q(b)) for a, b in g.resolved_pairs(s.id, depth))
    unresolved = [{"caller": q(u["caller"]), "text": u["text"], "line": u["line"], "reason": u["reason"],
                   "candidates": [q(c) for c in u.get("candidates", [])]} for u in g.unresolved_texts(s.id, depth)]
    known = sorted({sym.qualname for sym in g.symbols.values()})
    return {"symbol": s.id, "qualname": s.qualname, "depth": depth,
            "pairs": pairs, "unresolved": unresolved, "known_symbols": known,
            "tree": g.chain(s.id, depth)}


@mcp.tool()
def get_status(repo: str) -> dict:
    """저장소의 분석 상태(함수 수, 호출 수, 확인 안 됨 호출 수, 마지막 분석 시각)."""
    return _stats(repo, _graph(repo))


@mcp.tool()
def reanalyze(repo: str) -> dict:
    """분석 캐시를 버리고 저장소를 다시 분석한다(관리자 화면용). 저장소 파일은 건드리지 않는다."""
    registry.apply(POLICY)
    repo_root(repo, POLICY.allowed_repos)             # 허용 목록·경로 이탈 검사
    before = _stats(repo, _cache[repo]) if repo in _cache else None
    _cache.pop(repo, None)
    _docs.pop(repo, None)
    after = _stats(repo, _graph(repo))
    return {"before": before, "after": after}


@mcp.tool()
def forget_repo(repo: str) -> dict:
    """삭제된 런타임 저장소의 그래프·문서 색인·메모리 허용 목록을 비운다."""
    if repo in load_policy().allowed_repos or repo in registry.registered():
        raise ValueError("repository must be unregistered first")
    _cache.pop(repo, None)
    _docs.pop(repo, None)
    _analyzed_at.pop(repo, None)
    _revisions.pop(repo, None)
    registry.forget_policy(POLICY, repo)
    return {"repo": repo, "cached": repo in _cache or repo in _docs or repo in _analyzed_at,
            "allowed": repo in POLICY.allowed_repos}


@mcp.tool()
def search_docs(repo: str, query: str, k: int = 3) -> dict:
    """저장소 문서에서 질문과 관련된 절을 찾는다(키워드 검색: 이름 언급·토큰 겹침·'N단계' 번호)."""
    secs = _sections(repo)
    from backend.app.retrieval.hybrid import search
    hits, retrieval = search(repo,query,secs,max(1,min(k,10)))
    return {"repo":repo,"total_sections":len(secs),"hits":hits,"retrieval":retrieval}


@mcp.tool()
def list_doc_sections(repo: str, file: str = "") -> dict:
    """문서 절 메타데이터. file을 비우면 전체 문서 목록, 지정하면 그 파일의 모든 절을 돌려준다."""
    items = [{"id": s.id, "file": s.file, "title": s.title, "line": s.line,
              "score": 0.0, "preview": s.text[:120]} for s in _sections(repo) if not file or s.file == file]
    return {"repo": repo, "file": file, "hits": items}


@mcp.tool()
def get_doc_section(repo: str, section_id: str) -> dict:
    """문서 절 본문(마스킹·길이 제한 적용)과 절에 적힌 코드 이름·호출 관계 주장."""
    sec = _section(repo, section_id)
    text, masked = mask(sec.text[: POLICY.max_snippet_chars])
    return {"id": sec.id, "file": sec.file, "title": sec.title, "line": sec.line,
            "text": text, "masked": masked, "truncated": len(sec.text) > POLICY.max_snippet_chars,
            "mentions": sec.mentions, "claims": [list(c) for c in sec.claims]}


@mcp.tool()
def check_doc_section(repo: str, section_id: str, symbol: str = "") -> dict:
    """문서 절에 적힌 호출 관계를 호출 그래프와 대조한다(결정적, LLM 없음).
    판정: match / indirect / mismatch / missing(코드에 없는 이름) / unknown(해석 못 한 호출·모호).
    symbol을 주면 그 함수가 이 절과 코드상 어떻게 이어지는지(direct/linked/none)도 돌려준다."""
    g = _graph(repo)
    out = docmod.check_section(g, _section(repo, section_id), POLICY.max_depth)
    if symbol:
        s = _resolve(g, symbol)
        out["relation"] = docmod.relate_symbol(g, s.qualname, out, POLICY.max_depth)
    return out


@mcp.tool()
def get_group(repo: str, group: str) -> dict:
    """구조 개요의 묶음(클래스·모듈) 하나를 펼친다: 그 안의 함수들과 그 함수들이 부르는 호출(정적 분석)."""
    g = _graph(repo)
    try:
        return g.group_detail(group)
    except KeyError:
        raise ValueError(f"group not found: {group}")


@mcp.tool()
def get_overview(repo: str) -> dict:
    """클래스·모듈 단위 구조 개요(노드, 의존 간선, 노드별 미해석 호출 수). 정적 분석 결과의 집계다."""
    g = _graph(repo)
    ov = g.overview()
    ov["repo"] = repo
    ov["function_count"] = len(g.symbols)
    return ov


@mcp.tool()
def get_callers(repo: str, symbol: str, depth: int = 3) -> dict:
    """symbol을 직접·간접으로 호출하는 쪽을 거슬러 올라간 목록 — '이 함수를 바꾸면 영향받는 곳'."""
    g = _graph(repo)
    s = _resolve(g, symbol)
    depth = max(1, min(depth, 6))
    q = lambda sid: g.symbols[sid].qualname
    pairs = [(q(a), q(b)) for a, b in g.callers(s.id, depth)]
    affected = sorted({a for a, _ in pairs})
    return {"symbol": s.id, "qualname": s.qualname, "depth": depth,
            "pairs": pairs, "affected": affected}


@mcp.tool()
def get_source(repo: str, symbol: str) -> dict:
    """함수 본문을 반환한다. 경로 이탈을 막고, 길이를 제한하고, 비밀 값을 마스킹한 뒤 내보낸다."""
    g = _graph(repo)
    s = _resolve(g, symbol)
    root = repo_root(repo, POLICY.allowed_repos)
    path = safe_file(root, s.file)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(lines[s.start_line - 1:s.end_line])
    truncated = len(body) > POLICY.max_snippet_chars
    body = body[:POLICY.max_snippet_chars]
    masked, n = mask(body)
    return {"symbol": s.id, "file": s.file, "start_line": s.start_line,
            "source": masked, "masked_count": n, "truncated": truncated}


MAX_FILE_VIEW_CHARS = 200_000


@mcp.tool
def get_file(repo: str, symbol: str = "", path: str = "") -> dict:
    """파일 전체(사람이 보는 화면용, 에이전트 탐색에는 쓰지 않는다). symbol을 주면 그 함수가 든 파일과 줄 범위를,
    path를 주면 그 파일을 돌려준다. path는 분석이 인덱스한 소스 파일만 허용한다(임의 파일 읽기 차단)."""
    g = _graph(repo)
    hl = None
    if symbol:
        s = _resolve(g, symbol)
        rel, hl = s.file, [s.start_line, s.end_line]
    else:
        indexed = {s.file for s in g.symbols.values()}
        if path not in indexed:
            raise ValueError(f"not an indexed source file: {path!r}")
        rel = path
    root = repo_root(repo, POLICY.allowed_repos)
    text = safe_file(root, rel).read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_FILE_VIEW_CHARS
    masked, n = mask(text[:MAX_FILE_VIEW_CHARS])
    funcs = sorted(({"qualname": s.qualname, "start_line": s.start_line, "end_line": s.end_line}
                    for s in g.symbols.values() if s.file == rel), key=lambda x: x["start_line"])
    return {"file": rel, "start_line": 1, "source": masked, "masked_count": n, "truncated": truncated,
            "highlight": hl, "functions": funcs, "line_count": masked.count("\n") + 1}


@mcp.resource("repo://{repo}/symbols")
def symbols_resource(repo: str) -> str:
    """인덱스된 함수 목록(정적 문서)."""
    g = _graph(repo)
    return "\n".join(f"{s.qualname}\t{s.file}:{s.start_line}" for s in g.symbols.values())


@mcp.prompt()
def code_trace_prompt(repo: str, question: str) -> str:
    """호출 흐름 설명의 표준 절차 — plan 단계가 이 절차를 따른다."""
    return f"""저장소 '{repo}'의 코드에 대한 질문을 표준 절차로 처리하세요.

## 질문
{question}

## 절차 (반드시 순서대로)
1. list_functions로 진입점 후보를 찾으세요.
2. get_callees로 호출 대상을 따라가세요 (깊이와 호출 횟수에는 상한이 있습니다).
3. 필요한 함수만 get_source로 읽으세요.
4. 설명에는 '구조(검증 대상)'와 '해석(검증 안 됨)'을 구분하고, 호출 관계는 caller→callee 쌍 목록(claims)으로 함께 내세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
