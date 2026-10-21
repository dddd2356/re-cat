"""Python 분석기 — 표준 라이브러리 ast만 사용한다.

해석 규칙:
  - self.method()                → 같은 클래스(또는 그 부모 클래스)의 method
  - self.attr.method()           → self.attr의 타입(생성자 대입·타입 힌트로 추론)의 method
  - a.b.c.method()               → a부터 타입을 한 단계씩 따라가며 해석(중간에 타입을 모르면 중단)
  - 지역 변수·매개변수 x.method() → x의 타입(함수 인자 타입 힌트, 생성자 대입)의 method
  - func()                       → 같은 모듈 함수, 없으면 import 된 이름
  - Foo.method()/Foo()           → 클래스 이름이 알려져 있으면 해당 클래스(부모의 생성자/메서드 포함)
  - Foo().method()/mod.Foo().method() → 생성자가 만든 클래스의 method(부모 포함) — 변수에 담지 않아도 해석한다
  - mod.func() / mod.Cls()       → `from pkg import mod` 또는 `import pkg.mod as m`로 들어온 저장소 안 모듈의 함수·클래스
  - getattr(obj, "prefix" + x)   → prefix로 시작하는 저장소 안 메서드가 1개 이상이면 candidates
  - 타입 힌트가 Any·object·없음  → "타입을 안다"가 아니라 "모른다"로 취급(아래 수신자 모를 때 규칙을 탄다).
    SupervisorDeps처럼 백엔드 교체용으로 일부러 Any를 쓰는 선언은 그대로 두고, 분석기가 그런 코드도 다룬다.
  - 수신자 타입을 모르는 메서드 호출 → 그 이름을 가진 저장소 안 클래스가 1~3개면 candidates(런타임에 결정),
    4개 이상이면 unknown_receiver(기존과 동일). 내장 메서드 이름(list·dict·set·str·bytes·io·re에서 뽑음)과 겹치는데
    저장소 안 클래스도 그 이름의 메서드를 가지고 있으면 조용히 지우지 않고 reason="builtin_name_collision"으로 남긴다.
  - 수신자가 리터럴([] {} "" ())·내장 생성자(list()·dict()·open() 등)·re.compile()/re.match() 결과면
    내장 타입으로 확정해 external로 보낸다(후보로 흘리지 않는다).
  - 타입은 알지만(표준 라이브러리·서드파티 포함) 그 타입에 없는 메서드         → external(타입이 저장소 밖) 또는
    unknown_receiver(타입은 저장소 안인데 메서드가 없음) — 후보로 올리지 않는다(추측 금지)
  - 그 밖(getattr의 동적 부분, 콜백 인자, 데코레이터가 바꾸는 호출 등) → resolved=False
  - LangGraph 선언: g.add_node("n", fn) / add_edge("a","b") / add_conditional_edges("a", router, {..: "b"})
    → 노드 함수 사이의 실행 순서 간선(kind="graph")으로 추가한다. 빌더 함수는 시작 노드와 연결된다.
    (조건 분기의 매핑이 dict 리터럴이 아니면 resolved=False/dynamic으로 남긴다.)
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

from .base import BUILTIN_METHOD_NAMES, CallEdge, CallGraph, Symbol


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in fn.args.args]
    return f"{fn.name}({', '.join(args)})"


def _str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _module_dotted(rel: str) -> str:
    """파일 상대경로 → 점(.) 표기 모듈 경로. orders/__init__.py → "orders", orders/service.py → "orders.service"."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_dotted(rel: str) -> str:
    """이 파일이 속한 패키지의 점(.) 표기 경로(상대 import 기준점). __init__.py든 아니든 디렉터리 경로와 같다."""
    return ".".join(rel[:-3].split("/")[:-1])


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    """`from .x import y`류의 상대 import를 절대 점(.) 경로로 바꾼다 (level=1이면 현재 패키지 기준)."""
    bits = package.split(".") if package else []
    if level - 1 > len(bits):
        return None    # 최상위 패키지 밖으로 나가는 잘못된 상대 import
    base_bits = bits[: len(bits) - (level - 1)] if level > 1 else bits
    base = ".".join(base_bits)
    if module:
        return f"{base}.{module}" if base else module
    return base or None


_NO_HINT_TYPES = {"Any", "object"}    # 이것들은 "타입을 안다"가 아니라 "모른다"와 같다 — external로 보내지 않는다


def _type_name(ann: ast.AST | None) -> str | None:
    """타입 힌트 AST에서 수신자 타입으로 쓸 짧은 이름 하나를 뽑는다(제네릭 컨테이너는 원소 타입을 따라가지 않는다).
    Any·object·힌트 없음은 전부 None(타입 모름)으로 취급한다 — 수신자 타입을 모를 때의 규칙(candidates 등)을 타게 한다."""
    if ann is None:
        return None
    if isinstance(ann, ast.Name):
        return ann.id if ann.id not in _NO_HINT_TYPES else None
    if isinstance(ann, ast.Attribute):
        return ann.attr if ann.attr not in _NO_HINT_TYPES else None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        t = ann.value.split(".")[-1].split("[")[0].strip()    # 문자열 forward-ref
        return t if t not in _NO_HINT_TYPES else None
    if isinstance(ann, ast.Subscript):
        base = _type_name(ann.value)
        if base == "Optional":
            return _type_name(ann.slice)
        if base == "Union" and isinstance(ann.slice, ast.Tuple):
            for e in ann.slice.elts:
                t = _type_name(e)
                if t and t != "None":
                    return t
            return None
        return base    # List[Foo]/list[dict] 등 — 컨테이너 자체의 타입은 확정하되 원소 타입은 따라가지 않는다
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):    # PEP604: Foo | None
        left, right = _type_name(ann.left), _type_name(ann.right)
        if left and left != "None":
            return left
        return right if right != "None" else None
    return None


class _ModuleInfo:
    def __init__(self, rel: str, tree: ast.Module):
        self.rel = rel
        self.tree = tree
        self.functions: dict[str, ast.AST] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.imports: dict[str, str] = {}              # 로컬 이름 → 원래 이름 (심볼 단위, 참고용)
        self.module_imports: dict[str, str] = {}       # 로컬 별칭 → 모듈의 점(.) 표기 경로
        self._pending_from: list[tuple[str, str | None, str]] = []   # (별칭, base, 이름) — 모듈 확정 전 보류
        package = _package_dotted(rel)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:                        # 별칭 없는 'import pkg.mod'는 최상위 이름만 바인딩되어 범위 밖
                        self.module_imports[a.asname] = a.name
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_relative(package, node.level, node.module) if node.level else node.module
                for a in node.names:
                    local = a.asname or a.name
                    self.imports[local] = a.name
                    self._pending_from.append((local, base, a.name))   # 모듈인지 심볼인지는 2단계에서 확정

    def resolve_from_imports(self, module_by_dotted: dict[str, "_ModuleInfo"]) -> None:
        """ImportFrom으로 들어온 이름이 실제로 저장소 안 '모듈'(패키지의 하위 파일)일 때만 module_imports에 올린다.
        클래스·함수 같은 심볼 import까지 모듈로 오인하면 ClassName.method() 같은 정적 호출 해석이 깨진다."""
        for local, base, name in self._pending_from:
            candidate = f"{base}.{name}" if base else name
            if candidate in module_by_dotted:
                self.module_imports[local] = candidate


def _owner_candidates(attr: str, by_class: dict[str, dict[str, str]]) -> list[str]:
    if attr in BUILTIN_METHOD_NAMES:
        return []
    owners = [methods[attr] for methods in by_class.values() if attr in methods]
    return owners if 1 <= len(owners) <= 3 else []


def _has_builtin_name_collision(attr: str, by_class: dict[str, dict[str, str]]) -> bool:
    """내장 메서드 이름인데 저장소 안 클래스도 같은 이름의 메서드를 갖고 있다 — 조용히 external로 지우지 않는다."""
    return attr in BUILTIN_METHOD_NAMES and any(attr in methods for methods in by_class.values())


_BUILTIN_CTORS = {"list": "list", "dict": "dict", "set": "set", "tuple": "tuple", "str": "str",
                  "bytes": "bytes", "frozenset": "frozenset", "bytearray": "bytearray", "open": "file"}


def _builtin_type_name(expr: ast.AST) -> str | None:
    """수신자 식이 리터럴([] {} "" ())·내장 생성자(list()·open() 등)·re.compile()/re.match() 결과면
    내장 타입 이름을 돌려준다 — by_class에 없는 이름이라 이후 external로 확정된다(후보로 흘리지 않는다)."""
    if isinstance(expr, ast.List):
        return "list"
    if isinstance(expr, ast.Dict):
        return "dict"
    if isinstance(expr, ast.Set):
        return "set"
    if isinstance(expr, ast.Tuple):
        return "tuple"
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str):
            return "str"
        if isinstance(expr.value, bytes):
            return "bytes"
    if isinstance(expr, ast.Call):
        f = expr.func
        if isinstance(f, ast.Name) and f.id in _BUILTIN_CTORS:
            return _BUILTIN_CTORS[f.id]
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "re":
            if f.attr == "compile":
                return "Pattern"
            if f.attr in {"match", "search", "fullmatch"}:
                return "Match"
    return None


def _prefix_candidates(prefix: str, by_class: dict[str, dict[str, str]]) -> list[str]:
    return [sid for methods in by_class.values() for name, sid in methods.items() if name.startswith(prefix)]


def _getattr_prefix(node: ast.AST) -> str | None:
    """getattr(obj, "prefix" + x) 또는 getattr(obj, f"prefix{x}")에서 고정 prefix 문자열을 뽑는다."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _str(node.left) or None
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
            return first.value
    return None


def _lookup_method(cname: str, method: str, by_class: dict[str, dict[str, str]],
                    class_bases: dict[str, list[str]], seen: set[str] | None = None) -> str | None:
    """cname 자신의 메서드를 먼저 보고, 없으면 부모 클래스를 따라가며 찾는다(단순 선형 MRO)."""
    seen = seen if seen is not None else set()
    if cname in seen:
        return None
    seen.add(cname)
    sid = by_class.get(cname, {}).get(method)
    if sid:
        return sid
    for base in class_bases.get(cname, []):
        found = _lookup_method(base, method, by_class, class_bases, seen)
        if found:
            return found
    return None


def _root_is_self(expr: ast.AST) -> bool:
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return isinstance(expr, ast.Name) and expr.id == "self"


def _infer_type(expr: ast.AST, cname: str | None, var_types: dict[str, str], attr_types: dict[str, dict[str, str]],
                 module_imports: dict[str, str], by_class: dict[str, dict[str, str]],
                 module_by_dotted: dict[str, "_ModuleInfo"]) -> tuple[str, str] | None:
    """식 하나의 '타입'을 추론한다. ("class", 클래스이름) 또는 ("module", 점(.)표기경로) 또는 모르면 None."""
    bt = _builtin_type_name(expr)        # 리터럴([] {} "" ())·내장 생성자·re.compile()/re.match() 결과
    if bt:
        return ("class", bt)             # by_class에 없는 이름이라 이후 external로 확정된다
    if isinstance(expr, ast.Name):
        if expr.id == "self" and cname:
            return ("class", cname)
        if expr.id in module_imports:
            return ("module", module_imports[expr.id])
        if expr.id in var_types:
            return ("class", var_types[expr.id])
        if expr.id in by_class:                      # ClassName.method() 같은 정적 접근
            return ("class", expr.id)
        return None
    if isinstance(expr, ast.Attribute):
        base = _infer_type(expr.value, cname, var_types, attr_types, module_imports, by_class, module_by_dotted)
        if base is None or base[0] == "module":       # 모듈 속성 체인(mod.sub.x)은 더 들어가지 않는다
            return None
        attr_type = attr_types.get(base[1], {}).get(expr.attr)
        return ("class", attr_type) if attr_type else None
    if isinstance(expr, ast.Call):                     # Foo().method() / mod.Foo().method() — 생성자 결과
        f = expr.func
        if isinstance(f, ast.Name) and f.id in by_class:
            return ("class", f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in module_imports:
            mi = module_by_dotted.get(module_imports[f.value.id])
            if mi and f.attr in mi.classes:
                return ("class", f.attr)
        return None
    return None


def _var_types(fn: ast.AST, by_class: dict[str, dict[str, str]], module_imports: dict[str, str],
               module_by_dotted: dict[str, _ModuleInfo]) -> dict[str, str]:
    """함수 하나의 지역 타입 환경: 매개변수 타입 힌트 + 지역 변수의 생성자 대입·타입 힌트."""
    vt: dict[str, str] = {}
    args = fn.args
    for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
        if a.arg in ("self", "cls"):
            continue
        t = _type_name(a.annotation)
        if t:
            vt[a.arg] = t
    for node in ast.walk(fn):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            t = _type_name(node.annotation)
            if t:
                vt[node.target.id] = t
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)):
            vf = node.value.func
            type_name = _builtin_type_name(node.value)
            if type_name is None and isinstance(vf, ast.Name) and vf.id in by_class:
                type_name = vf.id
            elif type_name is None and isinstance(vf, ast.Attribute) and isinstance(vf.value, ast.Name) and vf.value.id in module_imports:
                mi = module_by_dotted.get(module_imports[vf.value.id])
                if mi and vf.attr in mi.classes:
                    type_name = vf.attr
            if type_name:
                vt[node.targets[0].id] = type_name
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)):
            bt = _builtin_type_name(node.value)     # x = [] / {} / "" / () 같은 리터럴 대입
            if bt:
                vt[node.targets[0].id] = bt
    return vt


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

    module_by_dotted = {_module_dotted(m.rel): m for m in modules}
    for m in modules:
        m.resolve_from_imports(module_by_dotted)

    # 1) 심볼 수집
    by_class: dict[str, dict[str, str]] = {}   # 클래스명 → {메서드명: symbol id}
    by_func: dict[str, str] = {}               # 모듈 함수명 → symbol id (이름 충돌 시 첫 번째)
    class_bases: dict[str, list[str]] = {}      # 클래스명 → 부모 클래스명 목록(알 수 있는 것만)
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
            bases = []
            for b in cls.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            class_bases[cname] = bases

    # 2) 클래스별 속성 타입: 생성자 대입(self.x = Foo()/mod.Foo()) + 타입 힌트(self.x: Foo, 클래스·dataclass 필드 x: Foo)
    attr_types: dict[str, dict[str, str]] = {}
    for m in modules:
        for cname, cls in m.classes.items():
            types = attr_types.setdefault(cname, {})
            for item in cls.body:     # 클래스 바로 밑의 필드 선언(dataclass 등) — 메서드 안의 지역 변수와 섞이지 않는다
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    t = _type_name(item.annotation)
                    if t:
                        types[item.target.id] = t
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign) and len(node.targets) >= 1:
                    type_name = _builtin_type_name(node.value)   # self.x = [] / {} / Foo() / re.compile(...)
                    if type_name is None and isinstance(node.value, ast.Call):
                        vf = node.value.func
                        if isinstance(vf, ast.Name):
                            type_name = vf.id
                        elif isinstance(vf, ast.Attribute) and isinstance(vf.value, ast.Name) and vf.value.id in m.module_imports:
                            mi = module_by_dotted.get(m.module_imports[vf.value.id])
                            if mi and vf.attr in mi.classes:
                                type_name = vf.attr
                    if type_name:
                        for t in node.targets:
                            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                                types[t.attr] = type_name
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
                    if isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
                        t = _type_name(node.annotation)
                        if t:
                            types[node.target.attr] = t

    # 3) 호출 해석
    def resolve(call: ast.Call, cname: str | None, m: _ModuleInfo,
                var_types: dict[str, str]) -> tuple[str | None, str, str, tuple[str, ...]]:
        f = call.func
        text = ast.unparse(f)
        if isinstance(f, ast.Name):
            if f.id in m.functions:
                return f"{m.rel}::{f.id}", text, "", ()
            if f.id in by_func:
                return by_func[f.id], text, "", ()
            if f.id in by_class:                      # 생성자 호출(부모의 __init__ 포함)
                init = _lookup_method(f.id, "__init__", by_class, class_bases)
                return (init, text, "", ()) if init else (None, text, "external", ())
            if f.id == "getattr" and len(call.args) >= 2:
                prefix = _getattr_prefix(call.args[1])
                if prefix:
                    cands = _prefix_candidates(prefix, by_class)
                    if cands:
                        return None, text, "candidates", tuple(cands)
                return None, text, "dynamic", ()
            if f.id in {"eval", "exec", "__import__"}:
                return None, text, "dynamic", ()
            if hasattr(builtins, f.id):
                return None, text, "builtin", ()
            return None, text, "external", ()
        if isinstance(f, ast.Attribute):
            receiver = _infer_type(f.value, cname, var_types, attr_types, m.module_imports, by_class, module_by_dotted)
            if receiver is not None:
                kind, name = receiver
                if kind == "module":
                    mi = module_by_dotted.get(name)
                    if mi is None:
                        return None, text, "external", ()        # 표준 라이브러리·서드파티 모듈
                    if f.attr in mi.functions:
                        return f"{mi.rel}::{f.attr}", text, "", ()
                    if f.attr in mi.classes and f.attr in by_class:
                        init = _lookup_method(f.attr, "__init__", by_class, class_bases)
                        return (init, text, "", ()) if init else (None, text, "external", ())
                    return None, text, "unknown_receiver", ()     # 우리 모듈인데 알려진 이름이 아님
                if name not in by_class:
                    return None, text, "external", ()             # 표준 라이브러리·서드파티 타입
                sid = _lookup_method(name, f.attr, by_class, class_bases)
                return (sid, text, "", ()) if sid else (None, text, "unknown_receiver", ())
            # 수신자 타입을 전혀 모른다 — 이름을 가진 저장소 안 클래스가 1~3개면 '런타임에 결정'(candidates)
            cands = _owner_candidates(f.attr, by_class)
            if cands:
                return None, text, "candidates", tuple(cands)
            if _has_builtin_name_collision(f.attr, by_class):
                return None, text, "builtin_name_collision", ()   # 내장 이름과 겹침 — 조용히 external로 지우지 않는다
            default = "unknown_receiver" if _root_is_self(f.value) else "external"
            return None, text, default, ()
        return None, text, "dynamic", ()

    for m in modules:
        scopes: list[tuple[str | None, ast.AST, str]] = []
        for name, fn in m.functions.items():
            scopes.append((None, fn, f"{m.rel}::{name}"))
        for cname, cls in m.classes.items():
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scopes.append((cname, item, f"{m.rel}::{cname}.{item.name}"))
        for cname, fn, caller in scopes:
            var_types = _var_types(fn, by_class, m.module_imports, module_by_dotted)
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    callee, text, reason, candidates = resolve(node, cname, m, var_types)
                    if callee == caller or reason == "builtin":
                        continue   # 재귀·내장 함수는 체인을 부풀리지 않도록 제외
                    g.edges.append(CallEdge(caller, callee, text, node.lineno,
                                            resolved=callee is not None, reason=reason, candidates=candidates))
    _add_langgraph_edges(g, modules, by_func)
    return g


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
