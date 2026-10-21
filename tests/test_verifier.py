from backend.app.agents.verifier import verify

CHAIN = {
    "pairs": [["A.run", "B.step"], ["B.step", "C.leaf"]],
    "known_symbols": ["A.run", "B.step", "C.leaf", "D.other", "PaymentGateway.pay"],
    "unresolved": [{"caller": "A.run", "text": "gateway.pay", "line": 3, "reason": "ambiguous"}],
}


def test_verified_claim():
    v = verify([{"caller": "A.run", "callee": "B.step"}], CHAIN)
    assert v.ok and len(v.verified) == 1


def test_hallucinated_name_is_error():
    v = verify([{"caller": "A.run", "callee": "Ghost.call"}], CHAIN)
    assert not v.ok and len(v.hallucinated) == 1
    assert "Ghost.call" in v.feedback()


def test_existing_names_without_edge_is_wrong():
    v = verify([{"caller": "A.run", "callee": "D.other"}], CHAIN)
    assert not v.ok and len(v.wrong) == 1


def test_claim_overlapping_unresolved_call_is_unverifiable_not_error():
    v = verify([{"caller": "A.run", "callee": "PaymentGateway.pay"}], CHAIN)
    assert v.ok and len(v.unverifiable) == 1


def test_duplicate_claims_counted_once():
    v = verify([{"caller": "A.run", "callee": "B.step"}] * 3, CHAIN)
    assert len(v.verified) == 1


CHAIN_WITH_CANDIDATES = {
    "pairs": [["A.run", "B.step"]],
    "known_symbols": ["A.run", "B.step", "X.save", "Y.save", "D.other"],
    "unresolved": [{"caller": "A.run", "text": "obj.save", "line": 5, "reason": "candidates",
                    "candidates": ["X.save", "Y.save"]}],
}


def test_claim_matching_static_candidate_is_unverifiable_not_wrong():
    v = verify([{"caller": "A.run", "callee": "X.save"}], CHAIN_WITH_CANDIDATES)
    assert v.ok and len(v.unverifiable) == 1


def test_claim_not_in_candidate_list_is_still_wrong():
    # 후보 목록에 없는, 체인에도 없는 관계는 여전히 '틀림'이다 — 후보 처리 추가로 wrong 기준이 느슨해지면 안 된다
    v = verify([{"caller": "A.run", "callee": "D.other"}], CHAIN_WITH_CANDIDATES)
    assert not v.ok and len(v.wrong) == 1
