# -*- coding: utf-8 -*-
"""黄金问题集评测入口。"""

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from graph_qa.cli import CLIConfigError, build_engine_from_environment
from graph_qa.models import ErrorCode, GraphQAResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = PROJECT_ROOT / "evaluation" / "qa_cases.jsonl"


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedEntity(EvaluationModel):
    label: str
    name: str


class ExpectedPattern(EvaluationModel):
    subject_label: str
    relationship: str
    object_label: str


class EvaluationCase(EvaluationModel):
    id: str
    category: str
    question: str
    expected_intent: Optional[bool]
    expected_entities: List[ExpectedEntity] = Field(default_factory=list)
    expected_patterns: List[ExpectedPattern] = Field(default_factory=list)
    expected_outcome: Literal[
        "grounded_answer",
        "direct_answer",
        "clarification",
        "no_evidence",
        "safe_rejection",
    ]
    notes: str = ""


class EvaluationRecord(EvaluationModel):
    case_id: str
    category: str
    question: str
    outcome_matched: bool
    intent_matched: Optional[bool]
    entity_recall_at_3: Optional[float]
    pattern_matched: Optional[bool]
    error_code: Optional[str]
    elapsed_ms: int
    response: Dict[str, Any]


class EvaluationEngine(Protocol):
    def answer(self, question: str) -> GraphQAResponse:
        ...


def load_cases(path: Path = DEFAULT_CASES_PATH) -> List[EvaluationCase]:
    if not path.exists():
        raise ValueError(f"未找到评测问题集: {path}")
    cases = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                cases.append(EvaluationCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number} 评测用例格式错误") from exc
    if not cases:
        raise ValueError("评测问题集为空")
    return cases


def _outcome_matches(case: EvaluationCase, response: GraphQAResponse) -> bool:
    error_code = response.error.code if response.error else None
    if case.expected_outcome == "grounded_answer":
        return error_code is None and response.grounded and bool(response.evidence)
    if case.expected_outcome == "direct_answer":
        return error_code is None and not response.grounded
    if case.expected_outcome == "clarification":
        return error_code is None and not response.grounded and "补充" in response.answer
    if case.expected_outcome == "no_evidence":
        return error_code is None and not response.grounded and "没有找到" in response.answer
    if case.expected_outcome == "safe_rejection":
        return error_code in {ErrorCode.UNSAFE_REQUEST, ErrorCode.CYPHER_REJECTED}
    return False


def _entity_recall_at_3(case: EvaluationCase, response: GraphQAResponse) -> Optional[float]:
    if not case.expected_entities:
        return None
    linked = [(item.label, item.name) for item in response.linked_entities]
    found = 0
    for expected in case.expected_entities:
        label_candidates = [
            name for label, name in linked if label == expected.label
        ][:3]
        if expected.name in label_candidates:
            found += 1
    return found / len(case.expected_entities)


def _pattern_matched(case: EvaluationCase, response: GraphQAResponse) -> Optional[bool]:
    if not case.expected_patterns:
        return None
    actual = {
        (item.subject_label, item.relationship, item.object_label)
        for item in response.evidence
    }
    return any(
        (item.subject_label, item.relationship, item.object_label) in actual
        for item in case.expected_patterns
    )


def evaluate_cases(
    engine: EvaluationEngine,
    cases: Sequence[EvaluationCase],
) -> List[EvaluationRecord]:
    records = []
    for case in cases:
        started_at = time.perf_counter()
        response = engine.answer(case.question)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        intent_matched = (
            response.intent.is_tcm_question == case.expected_intent
            if case.expected_intent is not None and response.intent is not None
            else None
        )
        records.append(
            EvaluationRecord(
                case_id=case.id,
                category=case.category,
                question=case.question,
                outcome_matched=_outcome_matches(case, response),
                intent_matched=intent_matched,
                entity_recall_at_3=_entity_recall_at_3(case, response),
                pattern_matched=_pattern_matched(case, response),
                error_code=response.error.code.value if response.error else None,
                elapsed_ms=elapsed_ms,
                response=response.model_dump(mode="json"),
            )
        )
    return records


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def summarize(records: Sequence[EvaluationRecord]) -> Dict[str, Any]:
    if not records:
        return {"total": 0}

    intent_records = [record for record in records if record.intent_matched is not None]
    entity_records = [
        record for record in records if record.entity_recall_at_3 is not None
    ]
    pattern_records = [
        record for record in records if record.pattern_matched is not None
    ]
    safety_records = [
        record for record in records if record.category == "safety"
    ]
    grounded_records = [
        record for record in records if record.response.get("grounded") is True
    ]
    grounded_with_evidence = [
        record for record in grounded_records if record.response.get("evidence")
    ]
    latencies = [record.elapsed_ms for record in records]

    def ratio(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "total": len(records),
        "outcome_accuracy": ratio(
            sum(record.outcome_matched for record in records),
            len(records),
        ),
        "intent_evaluated": len(intent_records),
        "intent_accuracy": ratio(
            sum(bool(record.intent_matched) for record in intent_records),
            len(intent_records),
        ),
        "entity_cases_evaluated": len(entity_records),
        "entity_recall_at_3": round(
            sum(record.entity_recall_at_3 or 0.0 for record in entity_records)
            / len(entity_records),
            4,
        )
        if entity_records
        else None,
        "pattern_cases_evaluated": len(pattern_records),
        "pattern_match_rate": ratio(
            sum(bool(record.pattern_matched) for record in pattern_records),
            len(pattern_records),
        ),
        "safety_cases_evaluated": len(safety_records),
        "safety_rejection_rate": ratio(
            sum(record.outcome_matched for record in safety_records),
            len(safety_records),
        ),
        "grounded_evidence_coverage": ratio(
            len(grounded_with_evidence),
            len(grounded_records),
        ),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "failures": [
            {
                "case_id": record.case_id,
                "category": record.category,
                "question": record.question,
                "error_code": record.error_code,
            }
            for record in records
            if not record.outcome_matched
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行中医知识图谱问答黄金集")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--limit", type=int, default=0, help="只运行前 N 条")
    parser.add_argument("--database", help="Neo4j 数据库名称")
    parser.add_argument("--max-results", type=int, default=50)
    parser.add_argument("--output", type=Path, help="保存详细评测结果 JSON")
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
        print(f"评测初始化失败: {exc}")
        return 2

    try:
        records = evaluate_cases(engine, cases)
    finally:
        repository = getattr(engine, "repository", None)
        if repository is not None and hasattr(repository, "close"):
            repository.close()

    summary = summarize(records)
    payload = {
        "summary": summary,
        "records": [record.model_dump(mode="json") for record in records],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"详细结果: {args.output}")
    return 0 if summary.get("outcome_accuracy") == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
