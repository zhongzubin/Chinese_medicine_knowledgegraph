# -*- coding: utf-8 -*-
"""构建并汇总独立人工事实审核样本。"""

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

FACT_VERDICTS = {"correct", "partial", "incorrect"}
REVIEW_STATUSES = {"pending", "completed"}
MIN_REVIEWED_CASES = 50
MIN_FACT_ACCURACY = 0.95


def sample_evaluation_records(
    records: Sequence[Mapping[str, Any]],
    count: int,
    seed: int,
) -> List[Mapping[str, Any]]:
    """按类别轮转抽样，避免单一问题类型占满人工审核集。"""
    if count < 1:
        raise ValueError("人工审核样本数必须大于 0")
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record.get("category", "unknown")), []).append(record)
    for category, values in grouped.items():
        random.Random(f"{seed}:{category}").shuffle(values)

    categories = sorted(grouped)
    selected: List[Mapping[str, Any]] = []
    while len(selected) < min(count, len(records)):
        progressed = False
        for category in categories:
            values = grouped[category]
            if not values:
                continue
            selected.append(values.pop())
            progressed = True
            if len(selected) >= count:
                break
        if not progressed:
            break
    return selected


def build_review_records(
    evaluation: Mapping[str, Any],
    count: int = MIN_REVIEWED_CASES,
    seed: int = 20261108,
) -> List[Dict[str, Any]]:
    records = evaluation.get("records", [])
    if not isinstance(records, list):
        raise ValueError("评测文件必须包含 records 数组")
    sampled = sample_evaluation_records(records, count, seed)
    reviews = []
    for record in sampled:
        response = record.get("response") or {}
        reviews.append(
            {
                "case_id": record.get("case_id"),
                "category": record.get("category"),
                "question": record.get("question"),
                "graph_answer": response.get("answer"),
                "linked_entities": response.get("linked_entities", []),
                "evidence": response.get("evidence", []),
                "facts": response.get("facts", []),
                "aggregates": response.get("aggregates", []),
                "paths": response.get("paths", []),
                "verification": response.get("verification"),
                "review_status": "pending",
                "fact_verdict": None,
                "unsupported_claims": [],
                "source_disclosure_correct": None,
                "reviewer_role": "",
                "review_notes": "",
            }
        )
    return reviews


def summarize_reviews(reviews: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    values = list(reviews)
    completed = [
        item for item in values if item.get("review_status") == "completed"
    ]
    invalid_statuses = sorted(
        {
            str(item.get("review_status"))
            for item in values
            if item.get("review_status") not in REVIEW_STATUSES
        }
    )
    if invalid_statuses:
        raise ValueError(f"未知审核状态: {', '.join(invalid_statuses)}")
    invalid_verdicts = sorted(
        {
            str(item.get("fact_verdict"))
            for item in completed
            if item.get("fact_verdict") not in FACT_VERDICTS
        }
    )
    if invalid_verdicts:
        raise ValueError(f"未知事实审核结论: {', '.join(invalid_verdicts)}")

    verdict_counts = {
        verdict: sum(
            item.get("fact_verdict") == verdict for item in completed
        )
        for verdict in sorted(FACT_VERDICTS)
    }
    source_checks = [
        item.get("source_disclosure_correct")
        for item in completed
        if isinstance(item.get("source_disclosure_correct"), bool)
    ]
    unsupported_claim_count = sum(
        len(item.get("unsupported_claims") or []) for item in completed
    )
    fact_accuracy = (
        verdict_counts["correct"] / len(completed) if completed else None
    )
    source_disclosure_rate = (
        sum(source_checks) / len(source_checks) if source_checks else None
    )
    not_evaluated = []
    if len(completed) < MIN_REVIEWED_CASES:
        not_evaluated.append("reviewed_cases")
    if fact_accuracy is None:
        not_evaluated.append("fact_accuracy_rate")
    if source_disclosure_rate is None:
        not_evaluated.append("source_disclosure_rate")

    checks = {
        "reviewed_cases": {
            "value": len(completed),
            "threshold": MIN_REVIEWED_CASES,
            "passed": len(completed) >= MIN_REVIEWED_CASES,
        },
        "fact_accuracy_rate": {
            "value": fact_accuracy,
            "threshold": MIN_FACT_ACCURACY,
            "passed": (
                fact_accuracy >= MIN_FACT_ACCURACY
                if fact_accuracy is not None
                else None
            ),
        },
        "unsupported_claim_count": {
            "value": unsupported_claim_count,
            "threshold": 0,
            "passed": unsupported_claim_count == 0,
        },
        "source_disclosure_rate": {
            "value": source_disclosure_rate,
            "threshold": 1.0,
            "passed": (
                source_disclosure_rate == 1.0
                if source_disclosure_rate is not None
                else None
            ),
        },
    }
    return {
        "total": len(values),
        "completed": len(completed),
        "pending": len(values) - len(completed),
        "fact_verdicts": verdict_counts,
        "fact_accuracy_rate": fact_accuracy,
        "unsupported_claim_count": unsupported_claim_count,
        "source_disclosure_rate": source_disclosure_rate,
        "checks": checks,
        "not_evaluated_metrics": not_evaluated,
        "acceptance": {
            "passed": (
                not not_evaluated
                and all(
                    check["passed"] is True
                    for check in checks.values()
                )
            ),
            "failed_metrics": [
                name
                for name, check in checks.items()
                if check["passed"] is False
            ],
        },
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(value, ensure_ascii=False) for value in values
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立人工事实审核工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="从评测结果抽样生成待审核文件")
    build_parser.add_argument("--evaluation", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--count", type=int, default=MIN_REVIEWED_CASES)
    build_parser.add_argument("--seed", type=int, default=20261108)

    summary_parser = subparsers.add_parser("summarize", help="汇总已完成人工审核")
    summary_parser.add_argument("--reviews", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "build":
        reviews = build_review_records(
            _load_json(args.evaluation),
            count=args.count,
            seed=args.seed,
        )
        _write_jsonl(args.output, reviews)
        print(f"已生成 {len(reviews)} 条待审核记录: {args.output}")
        return 0

    summary = summarize_reviews(_load_jsonl(args.reviews))
    if args.output:
        _write_json(args.output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
