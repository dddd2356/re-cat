-- v1. 분석 그래프는 메모리 캐시. 코드 원본을 DB에 복제하지 않는다.
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS repository (
    name TEXT PRIMARY KEY CHECK (name ~ '^[A-Za-z0-9_.-]+$' AND name NOT IN ('.','..')),
    sensitive BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT NOT NULL CHECK (source IN ('policy','folder','upload')),
    registered_at DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleting')),
    revision BIGINT NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS message (
    id BIGSERIAL PRIMARY KEY,
    repo TEXT REFERENCES repository(name) ON DELETE CASCADE,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS message_thread_idx ON message(repo, thread_id, id);
CREATE TABLE IF NOT EXISTS audit_event (
    id BIGSERIAL PRIMARY KEY,
    ts DOUBLE PRECISION NOT NULL,
    event TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_event_time_idx ON audit_event(ts);
CREATE TABLE IF NOT EXISTS document_embedding (
    repo TEXT NOT NULL REFERENCES repository(name) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    PRIMARY KEY (repo, section_id, model)
);
-- 本문은 파일에서 읽고 벡터 DB에는 출처 ID/해시/벡터만 저장한다.
CREATE TABLE IF NOT EXISTS deletion_job (
    repo TEXT PRIMARY KEY REFERENCES repository(name) ON DELETE CASCADE,
    token TEXT NOT NULL,
    files INTEGER NOT NULL CHECK (files >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS legacy_import (
    source_hash TEXT PRIMARY KEY,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
