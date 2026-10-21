"""문서 키워드 + 로컬 bge-m3/pgvector RRF 검색. 코드 본문은 임베딩하지 않는다."""
import hashlib
import math
import os
from urllib.parse import urlparse
import httpx
from backend.app.repositories import db
from backend.app.security.masking import mask
from .docs import search_sections

DIMENSION = 1024


def _embed(texts):
    url = os.environ.get("OLLAMA_EMBED_URL", "http://127.0.0.1:11434/api/embed")
    if urlparse(url).hostname not in {"localhost","127.0.0.1","::1"}:
        raise ValueError("embedding endpoint must be local for this MVP")
    model = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
    response = httpx.post(url, json={"model":model,"input":texts,"truncate":False}, timeout=120)
    response.raise_for_status()
    values = response.json()["embeddings"]
    if len(values) != len(texts) or any(len(v)!=DIMENSION or not all(math.isfinite(x) for x in v) for v in values):
        raise ValueError("embedding count/dimension/nonfinite value mismatch")
    return values


def _vector(value):
    return "[" + ",".join(str(x) for x in value) + "]"


def ensure_index(repo, sections):
    model = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
    prepared = {s.id:mask(s.title + "\n" + s.text)[0] for s in sections}
    hashes = {sid:hashlib.sha256(text.encode()).hexdigest() for sid,text in prepared.items()}
    with db.connection() as conn:
        revision = conn.execute("SELECT revision FROM repository WHERE name=%s AND status='active'", (repo,)).fetchone()
        if revision is None:
            raise ValueError("repository not active")
        existing = dict(conn.execute("SELECT section_id,content_hash FROM document_embedding WHERE repo=%s AND model=%s", (repo,model)).fetchall())
    missing = [sid for sid,digest in hashes.items() if existing.get(sid)!=digest]
    # 모델 호출 중 DB 트랜잭션을 열어 두지 않는다.
    embedded = []
    for start in range(0,len(missing),16):
        ids = missing[start:start+16]
        embedded.extend(zip(ids,_embed([prepared[sid] for sid in ids])))
    with db.transaction() as conn:
        active = conn.execute("SELECT revision FROM repository WHERE name=%s AND status='active' FOR SHARE", (repo,)).fetchone()
        if active != revision:
            raise ValueError("repository changed while embedding")
        for sid,vector in embedded:
            conn.execute("INSERT INTO document_embedding(repo,section_id,content_hash,model,embedding) VALUES(%s,%s,%s,%s,%s::vector) "
                         "ON CONFLICT(repo,section_id,model) DO UPDATE SET content_hash=excluded.content_hash,embedding=excluded.embedding",
                         (repo,sid,hashes[sid],model,_vector(vector)))
        conn.execute("DELETE FROM document_embedding WHERE repo=%s AND (model<>%s OR NOT(section_id=ANY(%s)))", (repo,model,list(hashes)))
    return model


def search(repo, query, sections, top_k=3):
    method = os.environ.get("RECAT_DOC_SEARCH", "hybrid")
    keyword = search_sections(query,sections,max(top_k,20))
    if method == "keyword" or not sections:
        return keyword[:top_k], {"method":"keyword", "vector_used":False}
    if method != "hybrid":
        raise ValueError("RECAT_DOC_SEARCH must be hybrid or keyword")
    try:
        model=ensure_index(repo,sections)
        vector=_embed([mask(query)[0]])[0]
        with db.connection() as conn:
            vectors=conn.execute("SELECT section_id,1-(embedding <=> %s::vector) AS similarity FROM document_embedding "
                                 "WHERE repo=%s AND model=%s ORDER BY embedding <=> %s::vector LIMIT 20",
                                 (_vector(vector),repo,model,_vector(vector))).fetchall()
    except (httpx.HTTPError,ValueError,KeyError) as exc:
        # 대체 경로는 응답 이벤트에 명시. 벡터 검색했다고 표시하지 않는다.
        return keyword[:top_k], {"method":"keyword-fallback", "vector_used":False,"error":type(exc).__name__}
    scores={}
    lexical={row["id"]:row for row in keyword}
    semantic={sid:float(sim) for sid,sim in vectors}
    for ranking in ([row["id"] for row in keyword], [sid for sid,_ in vectors]):
        for rank,sid in enumerate(ranking,1):
            scores[sid]=scores.get(sid,0)+1/(60+rank)
    by_id={s.id:s for s in sections}
    ordered=sorted(scores, key=lambda sid:(-scores[sid],sid))[:top_k]
    hits=[]
    for sid in ordered:
        sec=by_id[sid]
        hits.append({"id":sid,"file":sec.file,"title":sec.title,"line":sec.line,
                     "score":lexical.get(sid,{}).get("score",0),
                     "hybrid_score":round(scores[sid],6),"similarity":semantic.get(sid),"preview":mask(sec.text[:120])[0]})
    return hits,{"method":"hybrid-rrf","vector_used":True,"model":model}
