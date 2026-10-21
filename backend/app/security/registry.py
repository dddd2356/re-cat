"""PostgreSQL 저장소 등록부. 정책 기본 저장소의 제거·완화는 금지한다."""
from __future__ import annotations
import re
import time
from psycopg.errors import UniqueViolation
from backend.app.repositories import db
from .path_guard import REPOS_ROOT
from .policy import Policy, load_policy

_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class RegistryError(ValueError):
    pass


def registered() -> dict[str, dict]:
    with db.connection() as conn:
        rows = conn.execute("SELECT name,sensitive,registered_at,source,revision FROM repository WHERE source <> 'policy' AND status='active'").fetchall()
    return {n: {"sensitive": s, "registered_at": t, "source": src, "revision": rev} for n,s,t,src,rev in rows}


def apply(policy: Policy) -> Policy:
    # 이전에 합친 런타임 목록을 되돌리고 DB의 현재 활성 목록을 반영한다.
    prior = getattr(policy, "_runtime_repos", set())
    prior_sensitive = getattr(policy, "_runtime_sensitive", set())
    policy.allowed_repos[:] = [n for n in policy.allowed_repos if n not in prior]
    policy.sensitive_repos[:] = [n for n in policy.sensitive_repos if n not in prior_sensitive]
    base = set(policy.allowed_repos)
    base_sensitive = set(policy.sensitive_repos)
    rows = registered()
    for name, meta in rows.items():
        if name not in policy.allowed_repos:
            policy.allowed_repos.append(name)
        if meta["sensitive"] and name not in policy.sensitive_repos:
            policy.sensitive_repos.append(name)
    policy._runtime_repos = set(rows) - base
    policy._runtime_sensitive = {n for n,m in rows.items() if m["sensitive"]} - base_sensitive
    return policy


def candidates(policy):
    if not REPOS_ROOT.is_dir():
        return []
    return sorted(p.name for p in REPOS_ROOT.iterdir() if p.is_dir() and not p.is_symlink()
                  and _NAME.fullmatch(p.name) and not p.name.startswith(".") and p.name not in policy.allowed_repos)


def register(policy, repo, sensitive=True, source="folder"):
    if not _NAME.fullmatch(repo) or repo in (".", ".."):
        raise RegistryError("invalid repo name")
    root = REPOS_ROOT / repo
    if root.is_symlink() or not root.is_dir() or root.resolve().parent != REPOS_ROOT.resolve():
        raise RegistryError("repo must be a folder directly under data/repos")
    if repo in policy.allowed_repos:
        raise RegistryError("repo already allowed")
    try:
        with db.connection() as conn:
            conn.execute("INSERT INTO repository(name,sensitive,source,registered_at) VALUES(%s,%s,%s,%s)",
                         (repo, bool(sensitive), source, time.time()))
    except UniqueViolation:
        raise RegistryError("repo already registered or deletion pending") from None
    apply(policy)
    return registered()[repo]


def forget_policy(policy, repo):
    for items in (policy.allowed_repos, policy.sensitive_repos):
        while repo in items:
            items.remove(repo)


def bump(repo):
    with db.connection() as conn:
        conn.execute("UPDATE repository SET revision=revision+1 WHERE name=%s AND status='active'", (repo,))


def revision(repo):
    with db.connection() as conn:
        row = conn.execute("SELECT revision FROM repository WHERE name=%s AND status='active'", (repo,)).fetchone()
    return row[0] if row else None
