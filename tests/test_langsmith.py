"""LangSmith 연동 — 피드백은 숫자만, 추적이 허용된 요청만 / 실험 업로드는 샘플 저장소만."""
import json
from pathlib import Path

import pytest

from tools.eval import langsmith_upload as up
from backend.app.observability.langsmith_feedback import FEEDBACK_KEYS, FeedbackSender, run_url, scores_from
from backend.app.security.policy import Policy


class FakeClient:
    def __init__(self, fail_keys=()):
        self.feedback, self.datasets, self.examples, self.fail = [], set(), [], set(fail_keys)

    def create_feedback(self, run_id, key, score=None, comment=None):
        if key in self.fail:
            raise RuntimeError("boom")
        self.feedback.append((run_id, key, score))

    def has_dataset(self, dataset_name):
        return dataset_name in self.datasets

    def create_dataset(self, name, description=None):
        self.datasets.add(name)

    def create_examples(self, dataset_name, examples):
        self.examples += examples


def test_feedback_scores_are_numbers_with_fixed_keys():
    s = scores_from({"verified": 3, "hallucinated": 1, "wrong": 0, "unverifiable": 0}, 2, "agent", 1)
    assert set(s) <= set(FEEDBACK_KEYS) and all(isinstance(v, float) for v in s.values())
    assert s["recat.verified_ratio"] == 0.75 and s["recat.caught_errors"] == 1 and s["recat.fallback"] == 0
    assert scores_from(None, 3, "no-llm-fallback", 0)["recat.fallback"] == 1


def test_sender_survives_failures():
    c = FakeClient(fail_keys={"recat.wrong"})
    snd = FeedbackSender(lambda: c)
    n = snd.send("run-1", scores_from({"verified": 1}, 1, "agent", 0))
    assert n == len(c.feedback) and snd.failed == 1 and all(r == "run-1" for r, _, _ in c.feedback)


def test_run_url_template(monkeypatch):
    assert run_url("abc") is None
    monkeypatch.setenv("LANGSMITH_RUN_URL", "https://ls.example/r/{run_id}")
    assert run_url("abc") == "https://ls.example/r/abc" and run_url(None) is None


def _result(backend="ollama"):
    row = lambda repo, q: {"repo": repo, "entry": "OrderService.create", "question": q, "truth_pairs": 4,
                           "entry_hit": True, "A": {"claims": 4, "verified": 3, "hallucinated": 1, "wrong": 0, "unverifiable": 0},
                           "B": {"claims": 4, "verified": 4, "hallucinated": 0, "wrong": 0, "unverifiable": 0}, "C": None}
    return {"backend": backend, "when": "t", "models": {"quality": "m"},
            "rows": [row("sample_py", "q1"), row("sample_java", "q2")]}


def test_upload_skips_sensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "__file__", str(tmp_path / "x.py"))
    (tmp_path / "results").mkdir()
    calls = []
    c = FakeClient()
    rec = up.upload(_result(), client=c, evaluate_fn=lambda target, **k: calls.append((target, k)),
                    policy=Policy(sensitive_repos=["sample_java"]))
    assert [e["inputs"]["repo"] for e in c.examples] == ["sample_py"]          # 민감 저장소 질문은 올리지 않음
    assert rec["skipped_sensitive"] == 1 and rec["experiments"] == ["re-cat-A-ollama", "re-cat-B-ollama"]  # C는 None이라 생략
    target, kw = calls[0]
    assert target({"question": "q1"})["hallucinated"] == 1 and kw["data"] == "re-cat-eval"
    assert {e.__name__ for e in kw["evaluators"]} == {"error_rate", "verified_ratio"}
    log = (tmp_path / "results" / "langsmith_uploads.jsonl").read_text(encoding="utf-8")
    assert json.loads(log)["dataset"] == "re-cat-eval"


def test_evaluators():
    class R: outputs = {"claims": 4, "verified": 3, "hallucinated": 1, "wrong": 0}
    assert up.error_rate(R, None)["score"] == 0.25 and up.verified_ratio(R, None)["score"] == 0.75


def test_stream_sends_feedback_only_when_traced(tmp_path, monkeypatch):
    import os
    import time as _t
    from fastapi.testclient import TestClient
    os.environ["LLM_BACKEND"] = "ollama"
    os.environ["RECAT_API_KEY"] = ""
    import server
    c = FakeClient()
    monkeypatch.setattr(server, "_FEEDBACK", FeedbackSender(lambda: c))

    def ask(traced: bool):
        monkeypatch.setattr(server, "_traced", lambda repo, sensitive: traced)
        with TestClient(server.app) as cl:
            body = cl.get("/chat/stream", params={"message": "OrderService.create 호출 흐름", "repo": "sample_py"}).text
        assert "[DONE]" in body
        for _ in range(50):
            if not traced or c.feedback:
                break
            _t.sleep(0.05)
        return [e for e in server._read_audit() if e["event"] == "chat_stream"][-1]

    off = ask(False)
    assert off["traced"] is False and "run_id" not in off and not c.feedback
    on = ask(True)
    keys = {k for _, k, _ in c.feedback}
    assert on["traced"] is True and {r for r, _, _ in c.feedback} == {on["run_id"]}
    assert {"recat.verified", "recat.caught_errors", "recat.explain_attempts"} <= keys
    assert dict((k, s) for _, k, s in c.feedback)["recat.caught_errors"] == 0.0
