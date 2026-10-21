from pkg.mod import Thing


class Holder:
    thing: Thing

    def __init__(self):
        self.thing = Thing()

    def use_field(self):
        return self.thing.run()


def use_param(t: Thing):
    return t.run()


def use_local():
    t = Thing()
    return t.run()


def use_external(items: list):
    return items.append(1)


class SelfUnknown:
    def __init__(self, dep):
        self.dep = dep   # 대입만으론 타입을 추론하지 않는다 — 추측 금지

    def call_unknown(self):
        return self.dep.mystery_method_xyz()
