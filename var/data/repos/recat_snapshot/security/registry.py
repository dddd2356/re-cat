"""런타임 저장소 등록부 — 관리자가 화면에서 등록한 저장소를 policy.yaml 위에 '더하기만' 한다.

규칙
- policy.yaml이 허용한 저장소와 민감 지정은 여기서 바꿀 수 없다(제거·완화 불가).
- 등록 후보는 data/repos/ 바로 아래 폴더뿐이다. 임의 경로는 받지 않는다.
- 등록 기본값은 민감(sensitive=True)이다 — 클라우드·LangSmith 차단.
서버와 MCP 서브프로세스가 같은 파일을 읽으므로, 등록 직후 두 쪽 모두에 반영된다.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from .path_guard import REPOS_ROOT
from .policy import Policy

_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def registry_path() -> Path:
    return Path(os.environ.get("RECAT_REGISTRY") or REPOS_ROOT.parent / "registry.json")


def _load() -> dict:
    try:
        raw = json.loads(registry_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) and isinstance(raw.get("repos"), dict) else {"repos": {}}
    except (OSError, json.JSONDecodeError):
        return {"repos": {}}


def registered() -> dict[str, dict]:
    return _load()["repos"]


def apply(policy: Policy) -> Policy:
    """등록부를 policy에 합친다(합집합). 여러 번 불러도 결과가 같다."""
    for name, meta in registered().items():
        if not _NAME.match(name):
            continue
        if name not in policy.allowed_repos:
            policy.allowed_repos.append(name)
        if meta.get("sensitive", True) and name not in policy.sensitive_repos:
            policy.sensitive_repos.append(name)
    return policy


def candidates(policy: Policy) -> list[str]:
    """아직 허용되지 않은 data/repos/ 하위 폴더 — 등록할 수 있는 후보."""
    if not REPOS_ROOT.is_dir():
        return []
    return sorted(p.name for p in REPOS_ROOT.iterdir()
                  if p.is_dir() and not p.is_symlink() and _NAME.match(p.name)
                  and not p.name.startswith(".") and p.name not in policy.allowed_repos)


class RegistryError(ValueError):
    pass


def register(policy: Policy, repo: str, sensitive: bool = True, source: str = "folder") -> dict:
    if not _NAME.match(repo) or repo in (".", ".."):
        raise RegistryError("invalid repo name")
    p = REPOS_ROOT / repo
    if p.is_symlink() or not p.is_dir() or p.resolve().parent != REPOS_ROOT.resolve():
        raise RegistryError("repo must be a folder directly under data/repos")
    if repo in policy.allowed_repos:
        raise RegistryError("repo already allowed")
    data = _load()
    data["repos"][repo] = {"sensitive": bool(sensitive), "registered_at": round(time.time(), 3), "source": source}
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=path.parent)   # 요청마다 다른 임시 파일
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    Path(tmp_name).replace(path)                        # 같은 폴더 안 rename — 읽는 쪽은 옛 파일이나 새 파일만 본다
    apply(policy)
    return data["repos"][repo]
