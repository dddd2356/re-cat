"""PostgreSQL 대화 이력. 실제 본문은 사내 DB에 저장, 감사 메타데이터와 구분한다."""
from backend.app.repositories import db


class PostgresSessionStore:
    def __init__(self):
        db.check()

    def load(self, thread_id: str, repo: str = "") -> list[dict]:
        with db.connection() as conn:
            rows = conn.execute("SELECT role,content FROM message WHERE thread_id=%s AND repo IS NOT DISTINCT FROM %s ORDER BY id",
                                (thread_id, repo or None)).fetchall()
        return [{"role": r, "content": c} for r, c in rows]

    def append(self, thread_id: str, role: str, content: str, repo: str = "") -> None:
        with db.connection() as conn:
            conn.execute("INSERT INTO message(thread_id,role,content,repo) VALUES(%s,%s,%s,%s)",
                         (thread_id, role, content, repo or None))

    def append_turn(self, thread_id, question, answer, repo):
        with db.transaction():
            self.append(thread_id, "user", question, repo)
            self.append(thread_id, "assistant", answer, repo)

    def delete_repo(self, repo: str, legacy_threads: tuple[str, ...] = ()) -> None:
        with db.connection() as conn:
            conn.execute("DELETE FROM message WHERE repo=%s OR (repo IS NULL AND thread_id=ANY(%s))", (repo, list(legacy_threads)))

    def close(self):
        pass  # 연결은 각 트랜잭션에서 닫힌다.
