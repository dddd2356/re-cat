"""테스트 전용 LLM double. 제품 패키지에는 포함하지 않는다."""
import re

def _usage(): return {"model":"test-mock","tokens":0,"usd":0.0}

class MockFast:
    name="test-fast"
    def __init__(self,*_args,**_kwargs): pass
    def route(self, q, document_candidates=None):
        from backend.app.agents.routing import route_by_rule
        return route_by_rule(q)
    def select_expansions(self,q,c): return (c,_usage())
    async def chat(self,q,h,on_token=None):
        s="안녕하세요! 코드의 호출 흐름이 궁금한 함수나 기능을 물어보세요."
        if on_token: on_token(s)
        return s,_usage()
    def expand_query(self,q): return ([],_usage())

class MockQuality:
    name="test-quality"
    def __init__(self,*_args,**_kwargs): pass
    async def explain(self,q,structure,snippets,attempt,feedback,on_token=None):
        claims=[{"caller":a,"callee":b} for a,b in structure["pairs"]]
        text="\n".join(f"{a}가 {b}를 호출합니다." for a,b in structure["pairs"]) or "호출이 없습니다."
        if on_token: on_token(text)
        return {"explanation":text,"claims":claims},_usage()
    async def explain_doc(self,q,section,check):
        claims=[{"caller":x["caller"],"callee":x["callee"]} for x in check["claims"] if x["verdict"]=="match"]
        note=" 담당자 확인이 필요합니다." if check["counts"].get("missing") or check["counts"].get("mismatch") else ""
        return {"explanation":f"문서 '{section['title']}' 절에 따르면:{note}","claims":claims},_usage()

def factory(): return "mock",MockFast(),MockQuality(),None,None
