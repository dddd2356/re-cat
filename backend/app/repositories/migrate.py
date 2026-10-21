"""python -m store.migrate: 명시적으로 스키마 생성. 기존 데이터 삭제 없음."""
from pathlib import Path
import time
from psycopg import sql
from . import db


def migrate():
    with db.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(727032601)")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(db.schema_name())))
        # schema.sql은 UTF-8 소스다. Windows 시스템 로캘(CP949 등)에 의존하지 않는다.
        conn.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
        from backend.app.security.policy import load_policy
        p = load_policy()
        for name in p.allowed_repos:
            conn.execute("INSERT INTO repository(name,sensitive,source,registered_at) VALUES(%s,%s,'policy',%s) "
                         "ON CONFLICT(name) DO UPDATE SET sensitive=repository.sensitive OR excluded.sensitive",
                         (name, name in p.sensitive_repos, time.time()))
    print("PostgreSQL schema ready: " + db.schema_name())


if __name__ == "__main__":
    migrate()
