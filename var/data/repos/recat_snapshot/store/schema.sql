-- Recat 코드 인덱스 스키마 (설계 초안 — 아직 서버에 연결되지 않음). 코드 본문은 저장하지 않는다.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE repository (
  id BIGSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  sensitive BOOLEAN NOT NULL DEFAULT FALSE,
  indexed_at TIMESTAMPTZ
);

CREATE TABLE symbol (
  id TEXT PRIMARY KEY,                 -- "orders/service.py::OrderService.create"
  repo_id BIGINT NOT NULL REFERENCES repository(id) ON DELETE CASCADE,
  qualname TEXT NOT NULL,
  language TEXT NOT NULL,
  file TEXT NOT NULL,
  start_line INT NOT NULL,
  end_line INT NOT NULL,
  signature TEXT NOT NULL,
  doc TEXT NOT NULL DEFAULT ''
);

CREATE TABLE call_edge (
  caller TEXT NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
  callee TEXT REFERENCES symbol(id) ON DELETE CASCADE,   -- 해석 못 하면 NULL
  callee_text TEXT NOT NULL,
  line INT NOT NULL,
  resolved BOOLEAN NOT NULL,
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX call_edge_caller_idx ON call_edge(caller);

CREATE TABLE symbol_embedding (
  symbol_id TEXT PRIMARY KEY REFERENCES symbol(id) ON DELETE CASCADE,
  embedding vector(768)                -- 진입점 탐색 전용 (호출 체인에는 쓰지 않는다)
);

CREATE TABLE audit_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  event TEXT NOT NULL,
  repo TEXT,
  meta JSONB NOT NULL DEFAULT '{}'     -- 메타데이터만, 코드 본문 금지
);

-- 호출 체인 조회 예시 (순환은 path로 차단)
-- WITH RECURSIVE chain AS (
--   SELECT caller, callee, ARRAY[caller] AS path, 1 AS depth FROM call_edge WHERE caller = $1 AND resolved
--   UNION ALL
--   SELECT e.caller, e.callee, c.path || e.caller, c.depth + 1
--   FROM call_edge e JOIN chain c ON e.caller = c.callee
--   WHERE e.resolved AND e.caller <> ALL(c.path) AND c.depth < $2
-- ) SELECT * FROM chain;
