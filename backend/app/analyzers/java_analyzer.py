"""Java 분석기 — tree-sitter-java로 구문 트리를 만들고 호출을 해석한다.

해석 규칙:
  - foo()                → 같은 클래스의 foo
  - field.bar() / this.field.bar() / param.bar() / local.bar()
                         → 선언된 타입 T의 bar (생성자 주입 필드는 필드 선언의 타입을 쓴다)
  - T가 인터페이스       → 구현 클래스가 1개면 그 구현으로, 2개 이상이면 ambiguous(판정 불가)
  - Type.staticCall()    → 알려진 클래스의 메서드
  - new Foo(...)         → Foo 생성자 (있을 때)
  - Method.invoke / Class.forName 등 리플렉션 → dynamic(판정 불가)
저장소 밖 라이브러리 호출(String, List 등)은 기록하지 않는다. 한계: 프록시·AOP·이벤트·
@Qualifier 선택·람다 안의 타입 추론은 해석하지 못한다.
"""
from __future__ import annotations

from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from .base import CallEdge, CallGraph, Symbol

_JAVA = Language(tree_sitter_java.language())
_REFLECTION = {"invoke", "forName", "newInstance", "getMethod", "getDeclaredMethod"}


def _text(n: Node | None, src: bytes) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace") if n else ""


def _walk(node: Node):
    yield node
    for c in node.children:
        yield from _walk(c)


def _type_name(n: Node | None, src: bytes) -> str:
    t = _text(n, src)
    return t.split("<")[0].strip()   # 제네릭 제거: List<Foo> → List


class _Cls:
    def __init__(self, name: str, kind: str, rel: str):
        self.name, self.kind, self.rel = name, kind, rel
        self.methods: dict[str, str] = {}      # 메서드명 → symbol id
        self.fields: dict[str, str] = {}       # 필드명 → 타입명
        self.implements: list[str] = []


def analyze_java(repo: str, root: Path) -> CallGraph:
    parser = Parser(_JAVA)
    g = CallGraph(repo=repo)
    classes: dict[str, _Cls] = {}
    parsed: list[tuple[str, bytes, Node]] = []

    for path in sorted(root.rglob("*.java")):
        rel = path.relative_to(root).as_posix()
        src = path.read_bytes()
        parsed.append((rel, src, parser.parse(src).root_node))

    # 1) 클래스·메서드·필드 수집
    for rel, src, tree in parsed:
        for node in _walk(tree):
            if node.type not in ("class_declaration", "interface_declaration"):
                continue
            name = _text(node.child_by_field_name("name"), src)
            cls = classes.setdefault(name, _Cls(
                name, "interface" if node.type == "interface_declaration" else "class", rel))
            for child in node.children:
                if child.type in ("super_interfaces", "interfaces"):
                    cls.implements += [_type_name(t, src) for t in _walk(child)
                                       if t.type == "type_identifier"]
            body = node.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type == "field_declaration":
                    ftype = _type_name(member.child_by_field_name("type"), src)
                    for d in member.children:
                        if d.type == "variable_declarator":
                            cls.fields[_text(d.child_by_field_name("name"), src)] = ftype
                elif member.type in ("method_declaration", "constructor_declaration"):
                    mname = _text(member.child_by_field_name("name"), src)
                    sid = f"{rel}::{name}.{mname}"
                    if sid in g.symbols:
                        continue   # 오버로드는 첫 선언만 기록(한계)
                    params = _text(member.child_by_field_name("parameters"), src)
                    doc = ""
                    prev = member.prev_sibling
                    if prev is not None and prev.type == "block_comment":
                        doc = _text(prev, src).strip("/* \n")
                    g.add_symbol(Symbol(sid, mname, f"{name}.{mname}", "java", rel,
                                        member.start_point[0] + 1, member.end_point[0] + 1,
                                        f"{mname}{params}", doc))
                    cls.methods[mname] = sid

    implementers: dict[str, list[str]] = {}
    for c in classes.values():
        for iface in c.implements:
            implementers.setdefault(iface, []).append(c.name)

    # 2) 호출 해석
    def resolve_on_type(tname: str, mname: str, text: str):
        cls = classes.get(tname)
        if cls is None:
            return None, "", ""                      # 라이브러리 타입 — 기록하지 않음
        if cls.kind == "interface":
            impls = implementers.get(tname, [])
            if len(impls) == 1:
                sid = classes[impls[0]].methods.get(mname)
                return (sid, "", "") if sid else (None, text, "unknown_receiver")
            if len(impls) > 1:
                return None, text, "ambiguous"
            return None, text, "unknown_receiver"
        sid = cls.methods.get(mname)
        return (sid, "", "") if sid else (None, text, "unknown_receiver")

    for rel, src, tree in parsed:
        for cnode in _walk(tree):
            if cnode.type != "class_declaration":
                continue
            cname = _text(cnode.child_by_field_name("name"), src)
            cls = classes[cname]
            body = cnode.child_by_field_name("body")
            if body is None:
                continue
            for member in body.children:
                if member.type not in ("method_declaration", "constructor_declaration"):
                    continue
                mname = _text(member.child_by_field_name("name"), src)
                caller = f"{rel}::{cname}.{mname}"
                if g.symbols.get(caller) is None:
                    continue
                scope: dict[str, str] = dict(cls.fields)
                for n in _walk(member):
                    if n.type in ("formal_parameter", "local_variable_declaration"):
                        t = _type_name(n.child_by_field_name("type"), src)
                        if n.type == "formal_parameter":
                            scope[_text(n.child_by_field_name("name"), src)] = t
                        else:
                            for d in n.children:
                                if d.type == "variable_declarator":
                                    scope[_text(d.child_by_field_name("name"), src)] = t
                for n in _walk(member):
                    line = n.start_point[0] + 1
                    if n.type == "object_creation_expression":
                        tname = _type_name(n.child_by_field_name("type"), src)
                        init = classes[tname].methods.get(tname) if tname in classes else None
                        if init and init != caller:
                            g.edges.append(CallEdge(caller, init, f"new {tname}", line, True))
                        continue
                    if n.type != "method_invocation":
                        continue
                    callee_name = _text(n.child_by_field_name("name"), src)
                    obj = n.child_by_field_name("object")
                    text = (_text(obj, src) + "." if obj else "") + callee_name
                    if callee_name in _REFLECTION:
                        g.edges.append(CallEdge(caller, None, text, line, False, "dynamic"))
                        continue
                    if obj is None:
                        sid = cls.methods.get(callee_name)
                        if sid and sid != caller:
                            g.edges.append(CallEdge(caller, sid, text, line, True))
                        continue
                    recv = _text(obj, src)
                    if obj.type == "field_access":
                        recv = _text(obj.child_by_field_name("field"), src)
                    tname = scope.get(recv) or (recv if recv in classes else None)
                    if not tname:
                        continue
                    sid, unresolved_text, reason = resolve_on_type(tname, callee_name, text)
                    if sid:
                        if sid != caller:
                            g.edges.append(CallEdge(caller, sid, text, line, True))
                    elif reason:
                        g.edges.append(CallEdge(caller, None, unresolved_text, line, False, reason))
    return g
