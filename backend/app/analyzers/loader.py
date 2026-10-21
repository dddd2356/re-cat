"""저장소 폴더를 읽어 언어별 분석기를 돌리고 하나의 CallGraph로 합친다."""
from __future__ import annotations

from pathlib import Path

from .base import CallGraph
from .java_analyzer import analyze_java
from .python_analyzer import analyze_python


def analyze_repo(repo: str, root: Path) -> CallGraph:
    g = CallGraph(repo=repo)
    for sub in (analyze_python(repo, root), analyze_java(repo, root)):
        g.symbols.update(sub.symbols)
        g.edges.extend(sub.edges)
    return g
