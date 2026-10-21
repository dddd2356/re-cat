"""Python 분석기 — 표준 라이브러리 ast만 사용한다.

해석 규칙(의도적으로 단순하게 유지):
  - self.method()      → 같은 클래스의 method
  - self.attr.method() → __init__에서 self.attr = Foo(...) 로 대입된 타입의 method
  - func()             → 같은 모듈 함수, 없으면 import 된 이름
  - Foo.method()/Foo() → 클래스 이름이 알려져 있으면 해당 클래스 (생성자는 __init__)
  - 그 밖(getattr, 콜백 인자, 데코레이터가 바꾸는 호출 등) → resolved=False
  - LangGraph 선언: g.add_node("n", fn) / add_edge("a","b") / add_conditional_edges("a", router, {..: "b"})
    → 노드 함수 사이의 실행 순서 간선(kind="graph")으로 추가한다. 빌더 함수는 시작 노드와 연결된다.
    (조건 분기의 매핑이 dict 리터럴이 아니면 resolved=False/dynamic으로 남긴다.)
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

from .base import CallEdge, CallGraph, Symbol


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in fn.args.args]
    return f"{fn.name}({', '.join(args)})"


class _ModuleInfo:
    def __init__(self, rel: str, tree: ast.Module):
        self.rel = rel
        self.tree = tree
        self.functions: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.imports: dict[str, str] = {}   # 로컬 이름 → 원래 이름
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.imports[a.asname or a.name] = a.name


def analyze_python(repo: str, root: Path) -> CallGraph:
    g = CallGraph(repo=repo)
    modules: list[_ModuleInfo] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "/." in f"/{rel}" or "__pycache__" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        modules.append(_ModuleInfo(rel, tree))

    # 1) 심볼 수집
    by_class: dict[str, dict[str, str]] = {}   # 클래스명 → {메서드명: symbol id}
    by_func: dict[str, str] = {}               # 모듈 함수명 → symbol id (이름 충돌 시 첫 번째)
    for m in modules:
        for name, fn in m.functions.items():
            sid = f"{m.rel}::{name}"
            g.add_symbol(Symbol(sid, name, name, "python", m.rel, fn.lineno,
                                fn.end_lineno or fn.lineno, _sig(fn), ast.get_docstring(fn) or ""))
            by_func.setdefault(name, sid)
        for cname, cls in m.classes.items():
            methods = by_class.setdefault(cname, {})
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sid = f"{m.rel}::{cname}.{item.name}"
                    g.add_symbol(Symbol(sid, item.name, f"{cname}.{item.name}", "python",
                                        m.rel, item.lineno, item.end_lineno or item.lineno,
                                        _sig(item), ast.get_docstring(item) or ""))
                    methods[item.name] = sid

    # 2) 클래스별 속성 타입 (self.x = Foo(...))
    attr_types: dict[str, dict[str, str]] = {}
    for m in modules:
        for cname, cls in m.classes.items():
            types = attr_types.setdefault(cname, {})
            for node in ast.walk(cls):
                if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)):
                    for t in node.targets:
                        if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                and t.value.id == "self"):
                            types[t.attr] = node.value.func.id

    # 3) 호출 해석
    def resolve(call: ast.Call, cname: str | None, m: _ModuleInfo) -> tuple[str | None, str, str]:
        f = call.func
        text = ast.unparse(f)
        if isinstance(f, ast.Name):
            if f.id in m.functions:
                return f"{m.rel}::{f.id}", text, ""
            if f.id in by_func:
                return by_func[f.id], text, ""
            if f.id in by_class:                      # 생성자 호출
                init = by_class[f.id].get("__init__")
                return (init, text, "") if init else (None, text, "external")
            if f.id in {"getattr", "eval", "exec", "__import__"}:
                return None, text, "dynamic"
            if hasattr(builtins, f.id):
                return None, text, "builtin"
            return None, text, "external"
        if isinstance(f, ast.Attribute):
            v = f.value
            if isinstance(v, ast.Name) and v.id == "self" and cname:
                sid = by_class.get(cname, {}).get(f.attr)
                return (sid, text, "") if sid else (None, text, "unknown_receiver")
            if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                    and v.value.id == "self" and cname):
                tname = attr_types.get(cname, {}).get(v.attr)
                if tname and f.attr in by_class.get(tname, {}):
                    return by_class[tname][f.attr], text, ""
                return None, text, "unknown_receiver"
            if isinstance(v, ast.Name) and v.id in by_class:
                sid = by_class[v.id].get(f.attr)
                return (sid, text, "") if sid else (None, text, "unknown_receiver")
            return None, text, "external"
        return None, text, "dynamic"

    for m in modules:
        scopes: list[tuple[str | None, ast.AST, str]] = []
        for name, fn in m.functions.items():
            scopes.append((None, fn, f"{m.rel}::{name}"))
        for cname, cls in m.classes.items():
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scopes.append((cname, item, f"{m.rel}::{cname}.{item.name}"))
        for cname, fn, caller in scopes:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    callee, text, reason = resolve(node, cname, m)
                    if callee == caller or reason == "builtin":
                        continue   # 재귀·내장 함수는 체인을 부풀리지 않도록 제외
                    g.edges.append(CallEdge(caller, callee, text, node.lineno,
                                            resolved=callee is not None, reason=reason))
    _add_langgraph_edges(g, modules, by_func)
    return g


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _node_func(expr: ast.AST, m: _ModuleInfo, by_func: dict[str, str]) -> str | None:
    """add_node의 두 번째 인자에서 노드 함수를 찾는다. node(fn), partial(fn, ...) 같은 감싸기도 따라간다."""
    if isinstance(expr, ast.Name):
        return f"{m.rel}::{expr.id}" if expr.id in m.functions else by_func.get(expr.id)
    if isinstance(expr, ast.Call):
        for a in expr.args:
            found = _node_func(a, m, by_func)
            if found:
                return found
    return None


def _add_langgraph_edges(g: CallGraph, modules: list[_ModuleInfo], by_func: dict[str, str]) -> None:
    for m in modules:
        for builder_name, fn in m.functions.items():
            attr_calls = [n for n in ast.walk(fn)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
            nodes: dict[str, str] = {}      # 그래프 노드 이름 → 함수 symbol id
            for c in attr_calls:
                if c.func.attr == "add_node" and len(c.args) >= 2 and _str(c.args[0]):
                    sid = _node_func(c.args[1], m, by_func)
                    if sid:
                        nodes[_str(c.args[0])] = sid
            if not nodes:
                continue
            builder = f"{m.rel}::{builder_name}"

            def link(src: str | None, dst: str, text: str, line: int) -> None:
                a = builder if src is None else nodes.get(src)
                b = nodes.get(dst)
                if a and b:
                    g.edges.append(CallEdge(a, b, text, line, resolved=True, kind="graph"))

            for c in attr_calls:
                at, line = c.func.attr, c.lineno
                first = _str(c.args[0]) if c.args else None
                if at == "set_entry_point" and first:
                    link(None, first, f"set_entry_point({first})", line)
                elif at == "add_edge" and len(c.args) >= 2 and _str(c.args[1]):
                    dst = _str(c.args[1])
                    if isinstance(c.args[0], ast.Name) and c.args[0].id == "START":
                        link(None, dst, f"add_edge(START->{dst})", line)
                    elif first:                # END로 가는 간선은 호출 관계가 아니므로 제외
                        link(first, dst, f"add_edge({first}->{dst})", line)
                elif at == "add_conditional_edges" and first:
                    router = ast.unparse(c.args[1]) if len(c.args) > 1 else "?"
                    mapping = c.args[2] if len(c.args) > 2 else None
                    if isinstance(mapping, ast.Dict):
                        for v in mapping.values:
                            if _str(v):
                                link(first, _str(v), f"conditional({router}):{first}->{_str(v)}", line)
                    elif nodes.get(first):     # 매핑이 리터럴이 아니면 분기 대상을 알 수 없다
                        g.edges.append(CallEdge(nodes[first], None, f"conditional({router})", line,
                                                resolved=False, reason="dynamic", kind="graph"))
