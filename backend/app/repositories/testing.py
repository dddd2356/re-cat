"""실제 PostgreSQL 임시 스키마. 프로덕션 저장 경로와 동일하고 종료 시 스키마만 제거."""
from contextlib import contextmanager
import os
import uuid
from unittest.mock import patch
from psycopg import sql
from . import db
from .migrate import migrate


@contextmanager
def isolated_database():
    name = "recat_eval_" + uuid.uuid4().hex
    with patch.dict(os.environ, {"RECAT_DB_SCHEMA":name}):
        migrate()
        try:
            yield name
        finally:
            with db.connection() as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
