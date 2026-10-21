"""MCP 분석 캐시 LRU 제한 회귀 테스트 — 저장소가 늘어나도 메모리가 무한히 커지지 않는지."""
from backend.app.analyzers.base import CallGraph

import mcp_server as M


def test_evict_lru_drops_oldest_first(monkeypatch):
    monkeypatch.setattr(M, "MAX_CACHED_REPOS", 2)
    M._cache.clear(); M._analyzed_at.clear(); M._docs.clear(); M._revisions.clear()
    for name in ("a", "b", "c"):
        M._cache[name] = CallGraph(repo=name)
        M._analyzed_at[name] = 0.0
        M._docs[name] = []
        M._revisions[name] = 1
        M._evict_lru()
    assert list(M._cache) == ["b", "c"]           # 한도(2)를 넘자 가장 오래된 'a'가 빠짐
    assert "a" not in M._analyzed_at and "a" not in M._docs and "a" not in M._revisions


def test_graph_access_refreshes_lru_order(monkeypatch):
    monkeypatch.setattr(M, "MAX_CACHED_REPOS", 2)
    M._cache.clear(); M._analyzed_at.clear(); M._docs.clear()
    M._graph("sample_py")
    M._graph("sample_java")
    M._graph("sample_py")                          # 다시 접근 → 맨 뒤로(가장 최근)
    assert list(M._cache) == ["sample_java", "sample_py"]
    M._cache["third"] = CallGraph(repo="third")     # 별도 저장소가 추가된 상황을 직접 모델링
    M._evict_lru()
    assert "sample_java" not in M._cache
    assert set(M._cache) == {"sample_py", "third"}
