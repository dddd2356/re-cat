"""정적 분석기 품질 리포트 — 저장소 하나의 호출 해석 현황을 센다.

분류: 확정(resolved=True) / 외부(reason=="external") / 확인 안 됨(그 밖의 reason, 이유별) / 후보(reason=="candidates").
"확인 안 됨" 중 reason=="builtin_name_collision"은 내장 메서드 이름과 겹치는데 저장소 안 클래스도 같은 이름의
메서드를 가진 경우다(조용히 외부로 지우지 않고 화면에 보이게 남긴 것) — 구체적으로 어떤 호출인지 따로 센다.
그리고 '외부'로 분류된 호출 중 마지막 이름이 저장소 안 함수 이름과 같은 것 — 분석기가 우리 코드 호출을
잘못 외부로 떨어뜨렸을 가능성이 있는 호출이다(내장 메서드와 이름이 겹치는 경우는 따로 표시한다. 수신자 타입이
리터럴·생성자·타입 힌트로 confident하게 알려진 경우는 collision 버킷으로 가지 않고 여기 남을 수 있다 — 그건
진짜 외부 호출이라 오분류가 아니다).

실행: python -m eval.analyzer_report <repo> [--root data/repos]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

# tools/eval/analyzer_report.py -> tools/eval -> tools -> project root
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.app.analyzers.base import BUILTIN_METHOD_NAMES   # noqa: E402
from backend.app.analyzers.loader import analyze_repo          # noqa: E402


def _short(text: str) -> str:
    return text.rsplit(".", 1)[-1]


def report(repo: str, root: Path) -> dict:
    g = analyze_repo(repo, root)
    internal_names = {s.name for s in g.symbols.values()}

    confirmed = external = candidates = 0
    unresolved_by_reason: Counter[str] = Counter()
    external_name_hits: Counter[str] = Counter()
    external_builtin_hits: Counter[str] = Counter()
    collision_hits: Counter[str] = Counter()

    for e in g.edges:
        if e.resolved:
            confirmed += 1
            continue
        if e.reason == "external":
            external += 1
            name = _short(e.callee_text)
            if name in internal_names:
                if name in BUILTIN_METHOD_NAMES:
                    external_builtin_hits[e.callee_text] += 1
                else:
                    external_name_hits[e.callee_text] += 1
            continue
        if e.reason == "candidates":
            candidates += 1
            continue
        unresolved_by_reason[e.reason] += 1
        if e.reason == "builtin_name_collision":
            collision_hits[e.callee_text] += 1

    return {
        "repo": repo,
        "symbols": len(g.symbols),
        "edges": len(g.edges),
        "confirmed": confirmed,
        "external": external,
        "candidates": candidates,
        "unresolved_total": sum(unresolved_by_reason.values()),
        "unresolved_by_reason": dict(sorted(unresolved_by_reason.items())),
        "builtin_name_collisions": dict(sorted(collision_hits.items(), key=lambda x: -x[1])),
        "external_misclassified": dict(sorted(external_name_hits.items(), key=lambda x: -x[1])),
        "external_misclassified_builtin_overlap": dict(sorted(external_builtin_hits.items(), key=lambda x: -x[1])),
    }


def lookup_call(r: dict, text_substr: str) -> str:
    """주어진 부분 문자열을 포함하는 호출이 어느 버킷에 있는지 찾는다(예: 'deps.session_store.append')."""
    buckets = [("builtin_name_collision(확인 안 됨)", r["builtin_name_collisions"]),
               ("external(오분류 후보, 내장 겹침 아님)", r["external_misclassified"]),
               ("external(오분류 후보, 내장 겹침)", r["external_misclassified_builtin_overlap"])]
    for label, d in buckets:
        for text, n in d.items():
            if text_substr in text:
                return f"{text} → {label}, {n}건"
    return f"{text_substr}: 찾지 못함(이 저장소에 없거나 다른 식으로 적힘)"


def print_report(r: dict) -> None:
    print(f"\n== {r['repo']} — 함수 {r['symbols']}개, 호출 {r['edges']}개 ==")
    print(f"확정 {r['confirmed']}  외부 {r['external']}  확인 안 됨 {r['unresolved_total']}  후보 {r['candidates']}")
    if r["unresolved_by_reason"]:
        print("확인 안 됨(이유별):", ", ".join(f"{k}={v}" for k, v in r["unresolved_by_reason"].items()))
    if r["builtin_name_collisions"]:
        n = sum(r["builtin_name_collisions"].values())
        print(f"  그중 builtin_name_collision {n}건(내장 메서드 이름과 겹치는데 저장소 안 클래스도 같은 이름의 메서드를 가짐):")
        print("   ", ", ".join(f"{k}({v})" for k, v in list(r["builtin_name_collisions"].items())[:20]))
    total_mis = sum(r["external_misclassified"].values())
    total_mis_builtin = sum(r["external_misclassified_builtin_overlap"].values())
    print(f"외부로 분류됐지만 저장소 안 함수 이름과 같은 호출: {total_mis}건"
          f" (+ 내장 메서드 이름과 겹치는 것 {total_mis_builtin}건 별도 — 수신자 타입이 확실히 내장이라 오분류 아님)")
    if r["external_misclassified"]:
        print("  예:", ", ".join(f"{k}({v})" for k, v in list(r["external_misclassified"].items())[:10]))
    if r["external_misclassified_builtin_overlap"]:
        print("  내장 메서드 이름 겹침 예:",
              ", ".join(f"{k}({v})" for k, v in list(r["external_misclassified_builtin_overlap"].items())[:10]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--root", default=str(ROOT / "var" / "data" / "repos"))
    ap.add_argument("--find", default="", help="특정 호출 텍스트(부분 일치)가 어느 버킷에 있는지 찾는다")
    args = ap.parse_args()
    r = report(args.repo, Path(args.root) / args.repo)
    print_report(r)
    if args.find:
        print(f"\n--find {args.find!r}:", lookup_call(r, args.find))


if __name__ == "__main__":
    main()
