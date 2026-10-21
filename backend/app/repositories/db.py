"""짧은 연결·트랜잭션, 격리 스키마. DB 장애 시 파일 저장소로 대체하지 않는다."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re
import psycopg
from psycopg import sql
from dotenv import load_dotenv

load_dotenv()
_current = ContextVar("recat_db_transaction", default=None)


def schema_name():
    name = os.environ.get("RECAT_DB_SCHEMA", "recat")
    if not re.fullmatch(r"recat(?:_[a-zA-Z0-9_]+)?", name):
        raise ValueError("RECAT_DB_SCHEMA must be recat or recat_<name>")
    return name


def open_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required; run docker compose up -d db and python -m store.migrate")
    try:
        conn = psycopg.connect(url, connect_timeout=5)
    except psycopg.Error:
        raise RuntimeError("PostgreSQL connection failed; check DATABASE_URL and database availability") from None
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema_name())))
    conn.execute("SET statement_timeout TO '30s'")
    return conn


@contextmanager
def connection():
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    with open_connection() as conn:
        yield conn


@contextmanager
def transaction():
    with connection() as conn:
        with conn.transaction():
            token = _current.set(conn)
            try:
                yield conn
            finally:
                _current.reset(token)


def check():
    with connection() as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE version=1").fetchone()
        if not row:
            raise RuntimeError("PostgreSQL schema migration is required")
