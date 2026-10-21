from pathlib import Path

from backend.app.analyzers.loader import analyze_repo

ROOT = Path(__file__).resolve().parent.parent / "var" / "data" / "repos"


def _pairs(g, qualname):
    s = g.find(qualname)[0]
    return {(g.symbols[a].qualname, g.symbols[b].qualname) for a, b in g.resolved_pairs(s.id, 3)}


def test_python_call_chain():
    g = analyze_repo("sample_py", ROOT / "sample_py")
    pairs = _pairs(g, "OrderService.create")
    assert ("OrderService.create", "StockService.decrease") in pairs
    assert ("OrderService.create", "PaymentClient.request") in pairs
    assert ("PaymentClient.request", "PaymentClient._post") in pairs
    assert ("OrderService.create", "send_receipt") in pairs


def test_python_dynamic_call_is_unresolved_not_dropped():
    g = analyze_repo("sample_py", ROOT / "sample_py")
    s = g.find("OrderService.run_hook")[0]
    reasons = {u["reason"] for u in g.unresolved_texts(s.id)}
    assert "dynamic" in reasons


def test_java_field_injection_resolves_class_dependency():
    g = analyze_repo("sample_java", ROOT / "sample_java")
    pairs = _pairs(g, "OrderService.create")
    assert ("OrderService.create", "StockService.decrease") in pairs
    assert ("OrderService.create", "Notifier.send") in pairs
    assert ("OrderService.create", "OrderService.validate") in pairs


def test_java_interface_with_two_impls_is_ambiguous():
    g = analyze_repo("sample_java", ROOT / "sample_java")
    s = g.find("OrderService.create")[0]
    ambiguous = [u for u in g.unresolved_texts(s.id) if u["reason"] == "ambiguous"]
    assert ambiguous and ambiguous[0]["text"].endswith("pay")
    # 모호한 호출은 어느 구현으로도 확정하지 않는다
    pairs = _pairs(g, "OrderService.create")
    assert not any(b.endswith(".pay") for _, b in pairs)



def test_overview_groups_by_class_and_counts_calls():
    g = analyze_repo("sample_py", ROOT / "sample_py")
    ov = g.overview()
    labels = {n["label"] for n in ov["nodes"]}
    assert {"OrderService", "StockService", "PaymentClient"} <= labels
    by_label = {n["id"]: n["label"] for n in ov["nodes"]}
    edges = {(by_label[e["source"]], by_label[e["target"]]): e["count"] for e in ov["edges"]}
    assert edges[("OrderService", "StockService")] >= 1
    assert all(e["source"] != e["target"] for e in ov["edges"])    # 같은 묶음 안의 호출은 간선이 아니다


def test_callers_walks_backwards():
    g = analyze_repo("sample_py", ROOT / "sample_py")
    s = g.find("StockService._load")[0]
    names = {g.symbols[a].qualname for a, _ in g.callers(s.id, 3)}
    assert "StockService.decrease" in names and "OrderService.create" in names   # 간접 호출자까지


# ---- 분석기 보완(a~e): analyzer_fixtures 저장소로 각 항목을 단위로 검증 ----

def _edge(g, caller_qualname, text):
    cid = g.find(caller_qualname)[0].id
    for e in g.edges:
        if e.caller == cid and e.callee_text == text:
            return e
    raise AssertionError(f"edge not found: {caller_qualname} -> {text}")


def _fixtures():
    return analyze_repo("analyzer_fixtures", ROOT / "analyzer_fixtures")


def test_a_from_pkg_import_mod_resolves_module_function():
    g = _fixtures()
    e = _edge(g, "use_from_import", "mod.helper")
    assert e.resolved and g.symbols[e.callee].qualname == "helper"


def test_a_import_pkg_mod_as_alias_resolves_module_function():
    g = _fixtures()
    e = _edge(g, "use_import_as", "m2.helper")
    assert e.resolved and g.symbols[e.callee].qualname == "helper"


def test_a_module_qualified_constructor_and_external_module_alias():
    g = _fixtures()
    ctor = _edge(g, "use_module_class", "mod.Thing")
    assert ctor.resolved and g.symbols[ctor.callee].qualname == "Thing.__init__"
    missing = _edge(g, "use_module_missing", "mod.absent_fn")
    assert not missing.resolved and missing.reason == "unknown_receiver"     # 내부 모듈인데 없는 이름
    ext = _edge(g, "use_external_module", "p.join")
    assert not ext.resolved and ext.reason == "external"                    # os.path는 저장소 밖


def test_b_subclass_self_call_resolves_to_parent_method():
    g = _fixtures()
    e = _edge(g, "Child.call_greet", "self.greet")
    assert e.resolved and g.symbols[e.callee].qualname == "Base.greet"


def test_b_missing_method_on_known_type_stays_unknown_receiver_not_candidates():
    g = _fixtures()
    e = _edge(g, "Child.call_missing", "self.totally_absent")
    assert not e.resolved and e.reason == "unknown_receiver" and not e.candidates


def test_c_param_and_local_var_type_hint_resolve_method_chain():
    g = _fixtures()
    assert _edge(g, "use_param", "t.run").resolved
    assert _edge(g, "use_local", "t.run").resolved
    assert g.symbols[_edge(g, "use_local", "t.run").callee].qualname == "Thing.run"


def test_c_field_type_hint_and_chain_through_self_attr():
    g = _fixtures()
    e = _edge(g, "Holder.use_field", "self.thing.run")
    assert e.resolved and g.symbols[e.callee].qualname == "Thing.run"


def test_c_known_builtin_type_is_external_not_guessed():
    g = _fixtures()
    e = _edge(g, "use_external", "items.append")
    assert not e.resolved and e.reason == "external"


def test_d_unknown_receiver_with_few_owners_becomes_candidates():
    g = _fixtures()
    e = _edge(g, "Runner.run_unknown", "obj.save")
    assert not e.resolved and e.reason == "candidates"
    names = {g.symbols[c].qualname for c in e.candidates}
    assert names == {"Saver1.save", "Saver2.save"}


def test_d_getattr_prefix_becomes_candidates():
    g = _fixtures()
    e = _edge(g, "Runner.run_dynamic", "getattr")
    assert not e.resolved and e.reason == "candidates"
    names = {g.symbols[c].qualname for c in e.candidates}
    assert names == {"Doer.do_thing", "Doer.do_other"}


def test_d_too_many_owners_falls_back_unchanged():
    g = _fixtures()
    many = _edge(g, "Runner.run_too_many", "obj.dup4")
    assert not many.resolved and many.reason == "external" and not many.candidates


def test_d_builtin_name_colliding_with_internal_method_is_flagged_not_hidden():
    g = _fixtures()
    clash = _edge(g, "Runner.run_builtin_name_clash", "obj.append")
    assert not clash.resolved and clash.reason == "builtin_name_collision" and not clash.candidates


def test_d_builtin_name_without_internal_collision_stays_external():
    g = _fixtures()
    no_clash = _edge(g, "Runner.run_builtin_name_no_clash", "obj.keys")
    assert not no_clash.resolved and no_clash.reason == "external"


def test_e_candidates_never_become_confirmed():
    g = _fixtures()
    assert all(e.resolved or not e.candidates or e.reason == "candidates" for e in g.edges)
    assert all(not (e.resolved and e.candidates) for e in g.edges)   # 확정이면서 후보를 들고 있는 간선은 없다


def test_any_and_object_hints_are_treated_as_unknown_not_external():
    g = _fixtures()
    any_field = _edge(g, "WithAny.call_unknown_any", "self.dep.save")
    assert not any_field.resolved and any_field.reason == "candidates"
    object_param = _edge(g, "WithAny.call_param_object", "x.save")
    assert not object_param.resolved and object_param.reason == "candidates"


def test_constructor_call_result_method_resolves_with_inheritance():
    g = _fixtures()
    e = _edge(g, "ctor_chain", "Child2().greet2")
    assert e.resolved and g.symbols[e.callee].qualname == "Base2.greet2"


def test_module_qualified_constructor_call_result_method_resolves():
    g = _fixtures()
    e = _edge(g, "ctor_chain_module", "mod.Thing().run")
    assert e.resolved and g.symbols[e.callee].qualname == "Thing.run"


def test_literal_and_builtin_ctor_receivers_are_external_not_guessed():
    g = _fixtures()
    for caller, text in [("literal_receivers", "[].append"), ("literal_receivers", "{}.get"),
                         ("literal_receivers", "''.join"), ("literal_receivers", "set().add"),
                         ("builtin_ctor_receivers", "list().append"), ("builtin_ctor_receivers", "dict().get")]:
        e = _edge(g, caller, text)
        assert not e.resolved and e.reason == "external" and not e.candidates


def test_re_compile_result_is_external_not_guessed():
    g = _fixtures()
    e = _edge(g, "regex_receiver", "p.match")
    assert not e.resolved and e.reason == "external"
