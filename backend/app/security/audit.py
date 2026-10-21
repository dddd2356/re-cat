"""PostgreSQL 감사 이벤트. 정해진 스칼라 메타데이터만 허용하며 본문·예외 문자열은 버린다."""
import time
from psycopg.types.json import Jsonb
from backend.app.repositories import db

# 값은 사용자 본문이 아닌 서버가 생성한 메타데이터여야 한다.
FIELDS = set("repo thread_id symbol path decision mode attempts message_chars stream error sensitive kind files bytes replace functions_before functions_after verified hallucinated wrong unverifiable caught caught_errors fallback explain_attempts verified_ratio run_id run_url traced feedback_sent feedback_error tool_calls masked_count reason".split())


def record(event: str, **meta):
    safe = {k:v for k,v in meta.items() if k in FIELDS and (v is None or isinstance(v,(str,int,float,bool)))}
    # reason은 외부 오류 문장이 들어올 수 있어 예외 분류로만 남긴다.
    safe.pop("reason", None)
    with db.connection() as conn:
        conn.execute("INSERT INTO audit_event(ts,event,meta) VALUES(%s,%s,%s)", (round(time.time(),3), event, Jsonb(safe)))


def read(n: int | None = None):
    with db.connection() as conn:
        if n is None:
            rows = conn.execute("SELECT ts,event,meta FROM audit_event ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT ts,event,meta FROM (SELECT id,ts,event,meta FROM audit_event ORDER BY id DESC LIMIT %s) e ORDER BY id", (max(1,min(n,1000)),)).fetchall()
    return [{"ts":ts,"event":event,**meta} for ts,event,meta in rows]
