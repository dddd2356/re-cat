import re
from typing import Any

from pkg import mod


class WithAny:
    dep: Any

    def __init__(self, dep: object):
        self.dep = dep

    def call_unknown_any(self):
        return self.dep.save()   # Any 필드 — "타입 모름" 규칙(candidates)을 타야 한다

    def call_param_object(self, x: object):
        return x.save()


class Base2:
    def greet2(self):
        return "hi"


class Child2(Base2):
    pass


def ctor_chain():
    return Child2().greet2()     # 생성자 호출 결과에 바로 메서드 — 상속 포함 확정


def ctor_chain_module():
    return mod.Thing().run()     # 모듈 경유 생성자 체인도 확정


def literal_receivers():
    a = [].append(1)
    b = {}.get("x")
    c = "".join(["a"])
    d = set().add(1)
    return a, b, c, d


def builtin_ctor_receivers():
    a = list().append(1)
    b = dict().get("x")
    return a, b


def regex_receiver():
    p = re.compile("x")
    return p.match("y")
