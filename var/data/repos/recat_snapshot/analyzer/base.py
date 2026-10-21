"""정적 분석 공통 인터페이스 — 언어와 무관하게 Symbol / CallEdge 두 가지로 표현한다.

호출 관계의 '정답'은 LLM이 아니라 이 모듈의 분석 결과다. 분석기가 해석하지 못한 호출은
버리지 않고 resolved=False + reason으로 남겨, 검증 노드가 '틀림'이 아니라
'판정 불가'로 다룰 수 있게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Symbol:
    id: str            # 저장소 안에서 유일한 식별자. 예: "orders/service.py::OrderService.create"
    name: str          # 짧은 이름. 예: "create"
    qualname: str      # 클래스 포함 이름. 예: "OrderService.create"
    language: str      # "python" | "java"
    file: str          # 저장소 루트 기준 상대 경로
    start_line: int
    end_line: int
    signature: str
    doc: str = ""


@dataclass(frozen=True)
class CallEdge:
    caller: str                 # Symbol.id
    callee: str | None          # Symbol.id (resolved=True일 때), 아니면 None
    callee_text: str            # 소스에 적힌 호출 표현. 예: "self.stock.decrease"
    line: int
    resolved: bool
    reason: str = ""            # 해석 못 한 이유: "dynamic" | "external" | "ambiguous" | "unknown_receiver"
    kind: str = "call"          # "call"(직접 호출) | "graph"(LangGraph add_edge 등 선언으로 연결된 실행 순서)


@dataclass
class CallGraph:
    repo: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: list[CallEdge] = field(default_factory=list)

    def add_symbol(self, s: Symbol) -> None:
        self.symbols[s.id] = s

    def callees(self, symbol_id: str) -> list[CallEdge]:
        return [e for e in self.edges if e.caller == symbol_id]

    def find(self, text: str) -> list[Symbol]:
        """qualname 또는 짧은 이름으로 심볼을 찾는다 (정확히 일치하는 것만)."""
        exact = [s for s in self.symbols.values() if s.id == text or s.qualname == text]
        if exact:
            return exact
        return [s for s in self.symbols.values() if s.name == text]

    def chain(self, symbol_id: str, max_depth: int = 4) -> dict:
        """symbol_id에서 시작하는 호출 체인을 트리로 펼친다 (순환은 'cycle'로 표시)."""
        def walk(sid: str, depth: int, seen: tuple) -> dict:
            node = {"symbol": sid, "calls": [], "unresolved": []}
            if depth >= max_depth:
                node["truncated"] = True
                return node
            for e in self.callees(sid):
                if not e.resolved:
                    if e.reason == "external":
                        continue   # 저장소 밖 호출은 체인의 빈틈이 아니므로 미해석 목록에 넣지 않는다
                    node["unresolved"].append(
                        {"text": e.callee_text, "line": e.line, "reason": e.reason})
                elif e.callee in seen:
                    node["calls"].append({"symbol": e.callee, "cycle": True})
                else:
                    node["calls"].append(walk(e.callee, depth + 1, seen + (e.callee,)))
            return node
        return walk(symbol_id, 0, (symbol_id,))

    def resolved_pairs(self, symbol_id: str, max_depth: int = 4) -> set[tuple[str, str]]:
        """체인에 들어 있는 (caller, callee) 쌍 전체 — 검증 노드가 쓰는 정답 집합."""
        pairs: set[tuple[str, str]] = set()
        def walk(node: dict) -> None:
            for c in node["calls"]:
                pairs.add((node["symbol"], c["symbol"]))
                if "calls" in c:
                    walk(c)
        walk(self.chain(symbol_id, max_depth))
        return pairs

    def unresolved_texts(self, symbol_id: str, max_depth: int = 4) -> list[dict]:
        out: list[dict] = []
        def walk(node: dict) -> None:
            for u in node["unresolved"]:
                out.append({"caller": node["symbol"], **u})
            for c in node["calls"]:
                if "calls" in c:
                    walk(c)
        walk(self.chain(symbol_id, max_depth))
        return out

    # ---- 구조 개요 / 역방향 조회 (모두 분석 결과에서만 만든다) ----
    def group_of(self, sid: str) -> tuple[str, str, str]:
        """심볼이 속한 묶음 → (묶음 id, 표시 이름, 종류). 클래스 메서드는 클래스, 모듈 함수는 파일."""
        s = self.symbols[sid]
        if "." in s.qualname:
            cls = s.qualname.split(".")[0]
            return f"{s.file}::{cls}", cls, "class"
        return s.file, s.file, "module"

    def overview(self) -> dict:
        """클래스·모듈 단위 의존 그래프. 간선은 '묶음 A의 함수가 묶음 B의 함수를 부른다'를 합산한 것이다.
        해석 못 한 호출은 간선이 될 수 없으므로 노드의 unresolved 개수로만 남긴다(외부 호출 제외)."""
        nodes: dict[str, dict] = {}
        for sid, s in self.symbols.items():
            gid, label, kind = self.group_of(sid)
            n = nodes.setdefault(gid, {"id": gid, "label": label, "kind": kind, "file": s.file,
                                       "language": s.language, "functions": [], "unresolved": 0})
            n["functions"].append({"id": sid, "qualname": s.qualname, "name": s.name})
        agg: dict[tuple[str, str], dict] = {}
        for e in self.edges:
            if e.caller not in self.symbols:
                continue
            src = self.group_of(e.caller)[0]
            if not e.resolved:
                if e.reason != "external":
                    nodes[src]["unresolved"] += 1
                continue
            dst = self.group_of(e.callee)[0]
            if src == dst:
                continue
            a = agg.setdefault((src, dst), {"source": src, "target": dst, "count": 0, "kinds": set()})
            a["count"] += 1
            a["kinds"].add(e.kind)
        for a in agg.values():   # 선언(graph)으로만 이어진 간선은 따로 구분한다
            a["kind"] = "graph" if a.pop("kinds") == {"graph"} else "call"
        return {"nodes": sorted(nodes.values(), key=lambda n: n["id"]),
                "edges": sorted(agg.values(), key=lambda a: (a["source"], a["target"]))}

    def group_detail(self, group_id: str) -> dict:
        """구조 개요의 한 묶음(클래스·모듈) 안으로 들어간 그림 — 묶음의 함수들과, 그 함수들이 부르는 호출.
        묶음 밖 함수는 kind='external'(어느 묶음인지 표시), 해석 못 한 호출은 kind='unresolved'로 남긴다."""
        members = [sid for sid in self.symbols if self.group_of(sid)[0] == group_id]
        if not members:
            raise KeyError(group_id)
        mset = set(members)
        nodes = {sid: {"id": sid, "qualname": self.symbols[sid].qualname, "kind": "symbol",
                       "line": self.symbols[sid].start_line, "file": self.symbols[sid].file} for sid in members}
        edges, seen = [], set()
        for e in self.edges:
            if e.caller not in mset:
                continue
            if not e.resolved:
                if e.reason == "external":
                    continue
                uid = f"?{e.caller}|{e.callee_text}"
                nodes.setdefault(uid, {"id": uid, "qualname": e.callee_text, "kind": "unresolved", "reason": e.reason})
                key = (e.caller, uid)
            else:
                if e.callee not in mset:
                    g = self.group_of(e.callee)
                    nodes.setdefault(e.callee, {"id": e.callee, "qualname": self.symbols[e.callee].qualname,
                                                "kind": "external", "group": g[1], "group_id": g[0]})
                key = (e.caller, e.callee)
            if key not in seen and key[0] != key[1]:
                seen.add(key)
                edges.append({"source": key[0], "target": key[1], "kind": e.kind})
        label = self.group_of(members[0])[1]
        return {"group": group_id, "label": label, "functions": len(members),
                "nodes": list(nodes.values()), "edges": edges}

    def callers(self, symbol_id: str, max_depth: int = 3) -> list[tuple[str, str]]:
        """symbol_id를 (간접 포함) 부르는 쪽으로 거슬러 올라간 (caller, callee) 쌍 — 변경 영향 파악용."""
        pairs: list[tuple[str, str]] = []
        seen = {symbol_id}
        frontier = [symbol_id]
        for _ in range(max_depth):
            nxt = []
            for target in frontier:
                for e in self.edges:
                    if e.resolved and e.callee == target:
                        pairs.append((e.caller, target))
                        if e.caller not in seen:
                            seen.add(e.caller)
                            nxt.append(e.caller)
            frontier = nxt
        return sorted(set(pairs))

