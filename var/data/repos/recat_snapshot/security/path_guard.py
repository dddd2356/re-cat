"""경로 이탈 방지 — 저장소 이름과 파일 경로가 허용된 루트 밖을 가리키지 못하게 한다."""
from __future__ import annotations

import re
from pathlib import Path

REPOS_ROOT = Path(__file__).resolve().parent.parent / "data" / "repos"
DOCS_ROOT = Path(__file__).resolve().parent.parent / "data" / "docs"      # 관리자가 저장소별로 올린 문서
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class PathEscape(ValueError):
    pass


def repo_root(repo: str, allowed: list[str]) -> Path:
    if not _REPO_NAME.match(repo) or repo in (".", ".."):
        raise PathEscape(f"invalid repo name: {repo!r}")
    if repo not in allowed:
        raise PermissionError(f"repo not allowed: {repo}")
    root = (REPOS_ROOT / repo).resolve()
    if REPOS_ROOT.resolve() not in root.parents:
        raise PathEscape(f"repo escapes root: {repo!r}")
    return root


def safe_file(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root != p and root not in p.parents:
        raise PathEscape(f"path escapes repo: {rel!r}")
    return p


def docs_dir(repo: str, allowed: list[str]) -> Path:
    """저장소별 업로드 문서 폴더(data/docs/<repo>). 저장소 이름 검사는 repo_root와 같다."""
    repo_root(repo, allowed)
    d = (DOCS_ROOT / repo).resolve()
    if DOCS_ROOT.resolve() not in d.parents:
        raise PathEscape(f"docs escapes root: {repo!r}")
    return d
