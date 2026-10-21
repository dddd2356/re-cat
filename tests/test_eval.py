from pathlib import Path

from backend.app.analyzers.loader import analyze_repo
from tools.eval.run_eval import ground_truth, score, summarize

ROOT = Path(__file__).resolve().parent.parent / "var" / "data" / "repos"


def test_score_counts_each_verdict():
    g = analyze_repo("sample_py", ROOT / "sample_py")
    truth = ground_truth(g, "OrderService.create")
    claims = [{"caller": "OrderService.create", "callee": "StockService.decrease"},   # 맞음
              {"caller": "OrderService.create", "callee": "AuditLogger.write"},       # 없는 이름
              {"caller": "StockService.exists", "callee": "PaymentClient.request"}]   # 이름은 있으나 관계 없음
    s = score(claims, truth)
    assert (s["verified"], s["hallucinated"], s["wrong"]) == (1, 1, 1)


def test_summarize_error_rate_ignores_skipped_arm():
    rows = [{"C": {"claims": 10, "verified": 9, "hallucinated": 1, "wrong": 0, "unverifiable": 0}},
            {"C": None}]
    s = summarize(rows, "C")
    assert s["n"] == 1 and s["error_rate"] == 0.1
