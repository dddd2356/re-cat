class Saver1:
    def save(self):
        return 1


class Saver2:
    def save(self):
        return 2


class Doer:
    def do_thing(self):
        return 1

    def do_other(self):
        return 2


class HasAppend:
    def append(self, x):
        return x


class A4:
    def dup4(self):
        return 1


class B4:
    def dup4(self):
        return 2


class C4:
    def dup4(self):
        return 3


class D4:
    def dup4(self):
        return 4


class Runner:
    def run_unknown(self, obj):
        return obj.save()

    def run_dynamic(self, obj, name):
        return getattr(obj, "do_" + name)()

    def run_builtin_name_clash(self, obj):
        return obj.append(1)

    def run_builtin_name_no_clash(self, obj):
        return obj.keys()

    def run_too_many(self, obj):
        return obj.dup4()
