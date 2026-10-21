"""재시작 후 재개 가능한 저장소 삭제. 파일/DB 분산 트랜잭션을 가장하지 않는다."""
import shutil
import uuid
from . import db
from .errors import DeletionError
from backend.app.security import registry, audit
from backend.app.security.path_guard import REPOS_ROOT, DOCS_ROOT


def _paths(repo, token):
    return [(base / repo, base / (".delete-" + token)) for base in (REPOS_ROOT, DOCS_ROOT)]


def begin(repo):
    with db.transaction() as conn:
        row = conn.execute("SELECT source,status FROM repository WHERE name=%s FOR UPDATE", (repo,)).fetchone()
        if not row:
            raise DeletionError("not_found")
        if row[0] == "policy":
            raise DeletionError("protected")
        old = conn.execute("SELECT token,files FROM deletion_job WHERE repo=%s", (repo,)).fetchone()
        if old:
            return old[0], old[1], False
        token = uuid.uuid4().hex
        paths = _paths(repo, token)
        for root, _ in paths:
            if root.is_symlink() or (root.exists() and root.resolve().parent != root.parent.resolve()):
                raise DeletionError("unsafe_path")
        files = sum(1 for root,_ in paths if root.exists() for f in root.rglob("*") if f.is_file() or f.is_symlink())
        conn.execute("UPDATE repository SET status='deleting' WHERE name=%s", (repo,))
        conn.execute("INSERT INTO deletion_job(repo,token,files) VALUES(%s,%s,%s)", (repo,token,files))
    return token, files, True


def stage(repo, token):
    for root, staged in _paths(repo, token):
        if root.exists():
            root.rename(staged)


def rollback(repo, token):
    for root, staged in reversed(_paths(repo, token)):
        if staged.exists() and not root.exists():
            staged.rename(root)
    with db.transaction() as conn:
        conn.execute("DELETE FROM deletion_job WHERE repo=%s", (repo,))
        conn.execute("UPDATE repository SET status='active' WHERE name=%s", (repo,))


def finish(repo, token, files):
    # 실패하면 job/status와 이력이 남고 같은 API/재시작에서 재시도한다.
    for root, staged in _paths(repo, token):
        for path in (root, staged):
            if path.is_symlink():
                raise RuntimeError("deletion path changed to a symlink")
            if path.exists():
                shutil.rmtree(path)
    with db.transaction() as conn:
        # 구형 이력의 해당 저장소 thread도 같이 정리한다.
        rows = conn.execute("SELECT DISTINCT meta->>'thread_id' FROM audit_event WHERE meta->>'repo'=%s", (repo,)).fetchall()
        threads = [r[0] for r in rows if r[0]]
        conn.execute("DELETE FROM message WHERE repo IS NULL AND thread_id=ANY(%s)", (threads,))
        conn.execute("DELETE FROM repository WHERE name=%s AND source <> 'policy'", (repo,))
        audit.record("repo_delete", repo=repo, files=files)


def recover():
    with db.connection() as conn:
        jobs = conn.execute("SELECT repo,token,files FROM deletion_job ORDER BY created_at").fetchall()
    for repo,token,files in jobs:
        finish(repo,token,files)
