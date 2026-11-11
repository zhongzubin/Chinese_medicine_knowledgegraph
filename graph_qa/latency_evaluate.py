# -*- coding: utf-8 -*-
"""Graph QA 工作流阶段耗时评测。"""

import argparse
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Mapping, Protocol, Sequence

from graph_qa.cli import CLIConfigError, build_engine_from_environment
from graph_qa.evaluate import DEFAULT_CASES_PATH, EvaluationCase, load_cases
from graph_qa.models import GraphQAResponse, StageTiming


class TimedEvaluationEngine(Protocol):
    def answer_with_timings(
        self,
        question: str,
        request_id: str | None = None,
    ) -> tuple[GraphQAResponse, List[StageTiming]]:
        ...


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 3)


def _distribution(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        return {
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    return {
        "mean": round(sum(values) / len(values), 3),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3),
    }


def summarize_latency(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    request_totals = [float(record["total_ms"]) for record in records]
    stages: Dict[str, List[StageTiming]] = {}
    for record in records:
        for timing in record.get("stage_timings", []):
            if isinstance(timing, StageTiming):
                item = timing
            else:
                item = StageTiming.model_validate(timing)
            stages.setdefault(item.stage, []).append(item)

    request_total_ms = sum(request_totals)
    stage_summaries = []
    for stage, values in stages.items():
        elapsed = [item.elapsed_ms for item in values]
        stage_total = sum(elapsed)
        stage_summaries.append(
            {
                "stage": stage,
                "calls": len(values),
                **_distribution(elapsed),
                "total_ms": round(stage_total, 3),
                "share": (
                    round(stage_total / request_total_ms, 4)
                    if request_total_ms
                    else 0.0
                ),
            }
        )
    stage_summaries.sort(key=lambda item: (-item["total_ms"], item["stage"]))
    return {
        "samples": len(records),
        "request_total_ms": {
            **_distribution(request_totals),
            "total": round(request_total_ms, 3),
        },
        "stages": stage_summaries,
    }


def evaluate_latency(
    engine: TimedEvaluationEngine,
    cases: Sequence[EvaluationCase],
) -> List[Dict[str, Any]]:
    records = []
    for case in cases:
        started = perf_counter()
        response, timings = engine.answer_with_timings(
            case.question,
            request_id=f"latency-{case.id}",
        )
        total_ms = (perf_counter() - started) * 1000
        records.append(
            {
                "case_id": case.id,
                "category": case.category,
                "question": case.question,
                "total_ms": round(total_ms, 3),
                "grounded": response.grounded,
                "error_code": response.error.code.value if response.error else None,
                "stage_timings": [
                    item.model_dump(mode="json") for item in timings
                ],
            }
        )
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评测中医图谱问答阶段耗时")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--limit", type=int, default=0, help="只运行前 N 条")
    parser.add_argument("--database", help="Neo4j 数据库名称")
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--output", type=Path, help="保存详细耗时 JSON")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    if args.max_results < 1:
        parser.error("--max-results 必须大于等于 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        cases = load_cases(args.cases)
        if args.limit:
            cases = cases[: args.limit]
        engine = build_engine_from_environment(args.database, args.max_results)
    except (ValueError, CLIConfigError) as exc:
        print(f"耗时评测初始化失败: {exc}")
        return 2

    try:
        records = evaluate_latency(engine, cases)
    finally:
        repository = getattr(engine, "repository", None)
        if repository is not None and hasattr(repository, "close"):
            repository.close()

    summary = summarize_latency(records)
    payload = {"summary": summary, "records": records}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"详细结果: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
