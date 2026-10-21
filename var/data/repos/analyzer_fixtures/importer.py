import os.path as p

import pkg.mod as m2
from pkg import mod


def use_from_import():
    return mod.helper(1)


def use_import_as():
    return m2.helper(2)


def use_module_class():
    t = mod.Thing()
    return t.run()


def use_module_missing():
    return mod.absent_fn()


def use_external_module():
    return p.join("a", "b")
