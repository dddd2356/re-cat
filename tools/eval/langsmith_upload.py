"""eval 결과를 LangSmith 데이터셋·실험으로 올린다 — A/B/C를 LangSmith 화면에서 나란히 비교하기 위해.

원칙
- 올리는 질문은 샘플 저장소(고객 코드가 아님)뿐이다. policy.yaml에서 민감으로 지정된 저장소의 행은 뺀다.
- 파이프라인을 다시 돌리지 않는다: run_eval.py가 이미 계산한 결과를 target이 그대로 돌려주고,
  평가자(evaluator)가 오류율·검증 비율을 점수로 붙인다.

사용: python -m eval.run_eval --backend ollama --langsmith
      python -m eval.langsmith_upload eval/results/<파일>.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Callable

from backend.app.security.policy import load_policy

DATASET = "re-cat-eval"
ARMS = {"A": "코드만 준 LLM", "B": "구조 제공·검증 없음", "C": "Re-cat 전체"}


class UploadRefused(RuntimeError):
    pass


def usable_rows(result: dict, sensitive: list[str]) -> list[dict]:
    return [r for r in result["rows"] if r["repo"] not in sensitive]


def build_examples(rows: list[dict]) -> list[dict]:
    return [{"inputs": {"question": r["question"], "repo": r["repo"]},
             "outputs": {"entry": r["entry"], "truth_pairs": r["truth_pairs"]},
             "metadata": {"source": "re-cat eval/questions.json"}} for r in rows]


def arm_outputs(rows: list[dict], arm: str) -> dict[str, dict]:
    out = {}
    for r in rows:
        s = r.get(arm)
        if s is None:
            continue
        out[r["question"]] = {k: s[k] for k in ("claims", "verified", "hallucinated", "wrong", "unverifiable")} | \
                             ({"entry_hit": r.get("entry_hit")} if arm != "A" else {})
    return out


def error_rate(run, example) -> dict:
    o = run.outputs or {}
    claims = o.get("claims", 0)
    return {"key": "error_rate", "score": None if not claims else round((o["hallucinated"] + o["wrong"]) / claims, 4)}


def verified_ratio(run, example) -> dict:
    o = run.outputs or {}
    claims = o.get("claims", 0)
    return {"key": "verified_ratio", "score": None if not claims else round(o["verified"] / claims, 4)}


def upload(result: dict, *, client=None, evaluate_fn: Callable | None = None,
           dataset_name: str = DATASET, policy=None) -> dict:
    policy = policy or load_policy()
    rows = usable_rows(result, policy.sensitive_repos)
    skipped = len(result["rows"]) - len(rows)
    if not rows:
        raise UploadRefused("올릴 수 있는 행이 없습니다 (민감 저장소 행은 제외됨)")
    if client is None:
        from langsmith import Client
        client = Client()
    if evaluate_fn is None:
        from langsmith import evaluate as evaluate_fn
    if not client.has_dataset(dataset_name=dataset_name):
        client.create_dataset(dataset_name, description="Re-cat 비교 실험 질문 (샘플 저장소만)")
        client.create_examples(dataset_name=dataset_name, examples=build_examples(rows))
    experiments = []
    meta = {"backend": result["backend"], "models": result.get("models"), "when": result.get("when")}
    for arm, label in ARMS.items():
        outs = arm_outputs(rows, arm)
        if not outs:
            continue
        evaluate_fn(lambda inputs, _o=outs: _o.get(inputs["question"], {"claims": 0, "verified": 0,
                                                                         "hallucinated": 0, "wrong": 0,
                                                                         "unverifiable": 0}),
                    data=dataset_name, evaluators=[error_rate, verified_ratio],
                    experiment_prefix=f"re-cat-{arm}-{result['backend']}",
                    metadata={**meta, "arm": arm, "arm_label": label}, client=client)
        experiments.append(f"re-cat-{arm}-{result['backend']}")
    record = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "dataset": dataset_name,
              "experiments": experiments, "rows": len(rows), "skipped_sensitive": skipped, **meta}
    log = Path(__file__).parent / "results" / "langsmith_uploads.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("result")
    a = ap.parse_args()
    print(upload(json.loads(Path(a.result).read_text(encoding="utf-8"))))
