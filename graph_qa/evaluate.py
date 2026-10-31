# -*- coding: utf-8 -*-
"""黄金问题集评测入口。"""

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from graph_qa.cli import CLIConfigError, build_engine_from_environment
from graph_qa.models import EntityCandidate, ErrorCode, GraphQAResponse, LinkedEntity

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = PROJECT_ROOT / "evaluation" / "qa_cases.jsonl"
MatchPath = Literal["exact", "fuzzy", "semantic"]
ACCEPTANCE_THRESHOLDS: Dict[str, float] = {
    "intent_accuracy": 0.90,
    "entity_recall_at_3": 0.85,
    "cypher_pass_rate": 0.90,
    "query_execution_success_rate": 0.95,
    "grounded_evidence_coverage": 1.0,
    "fact_match_rate": 0.90,
    "safety_rejection_rate": 1.0,
    "empty_result_honesty_rate": 1.0,
}
QUERY_EXECUTION_ERROR_CODES = {
    ErrorCode.QUERY_TIMEOUT,
    ErrorCode.QUERY_EXECUTION_FAILED,
}


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedEntity(EvaluationModel):
    label: str
    name: str


class ExpectedPattern(EvaluationModel):
    subject_label: str
    relationship: str
    object_label: str


class ExpectedFact(EvaluationModel):
    subject_label: str
    subject_name: str
    attribute: str
    value_contains: str


class EvaluationCase(EvaluationModel):
    id: str
    category: str
    question: str
    expected_intent: Optional[bool]
    expected_entities: List[ExpectedEntity] = Field(default_factory=list)
    expected_patterns: List[ExpectedPattern] = Field(default_factory=list)
    expected_facts: List[ExpectedFact] = Field(default_factory=list)
    expected_match_path: Optional[MatchPath] = None
    retrieval_probe: Optional[str] = None
    expected_outcome: Literal[
        "grounded_answer",
        "direct_answer",
        "clarification",
        "no_evidence",
        "safe_rejection",
    ]
    notes: str = ""

    @field_validator("retrieval_probe")
    @classmethod
    def strip_retrieval_probe(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("retrieval_probe 不能为空")
        return value


class EntityMatchRecord(EvaluationModel):
    expected_label: str
    expected_name: str
    expected_path: Optional[MatchPath]
    found_at_3: bool
    label_rank: Optional[int]
    actual_match_type: Optional[Literal["name", "alias", "fuzzy", "semantic"]]
    actual_path: Optional[MatchPath]
    path_matched: Optional[bool]
    score: Optional[float]


class EvaluationRecord(EvaluationModel):
    case_id: str
    category: str
    question: str
    outcome_matched: bool
    intent_matched: Optional[bool]
    entity_recall_at_3: Optional[float]
    entity_matches: List[EntityMatchRecord] = Field(default_factory=list)
    pattern_matched: Optional[bool]
    fact_matched: Optional[bool]
    unknown_entity_evaluated: bool
    cypher_passed: Optional[bool]
    query_execution_succeeded: Optional[bool]
    empty_result_honest: Optional[bool]
    error_code: Optional[str]
    elapsed_ms: int
    response: Dict[str, Any]


class RetrievalRecord(EvaluationModel):
    case_id: str
    category: str
    question: str
    retrieval_probe: str
    expected_entities: List[ExpectedEntity]
    expected_path: Optional[MatchPath]
    found_at_3: Optional[bool]
    label_rank: Optional[int]
    actual_match_type: Optional[Literal["name", "alias", "fuzzy", "semantic"]]
    actual_path: Optional[MatchPath]
    path_matched: Optional[bool]
    false_recall: Optional[bool]
    linked_entities: List[LinkedEntity] = Field(default_factory=list)


class EvaluationEngine(Protocol):
    def answer(self, question: str) -> GraphQAResponse:
        ...


class RetrievalIndex(Protocol):
    def match(
        self,
        candidates: Sequence[EntityCandidate],
        question: str,
        limit_per_label: int = 10,
        fuzzy_threshold: float = 0.72,
    ) -> List[LinkedEntity]:
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
        return (
            error_code is None
            and response.grounded
            and bool(response.evidence or response.facts)
        )
    if case.expected_outcome == "direct_answer":
        return error_code is None and not response.grounded
    if case.expected_outcome == "clarification":
        return error_code is None and not response.grounded and "补充" in response.answer
    if case.expected_outcome == "no_evidence":
        return error_code is None and not response.grounded and "没有找到" in response.answer
    if case.expected_outcome == "safe_rejection":
        return error_code in {ErrorCode.UNSAFE_REQUEST, ErrorCode.CYPHER_REJECTED}
    return False


def _match_path(match_type: str) -> Optional[MatchPath]:
    if match_type in {"name", "alias"}:
        return "exact"
    if match_type in {"fuzzy", "semantic"}:
        return match_type
    return None


def _evaluate_expected_entities(
    case: EvaluationCase,
    linked_entities: Sequence[LinkedEntity],
) -> tuple[List[EntityMatchRecord], Optional[float]]:
    if not case.expected_entities:
        return [], None

    first_by_key: Dict[tuple[str, str], tuple[LinkedEntity, int]] = {}
    label_counts: Dict[str, int] = {}
    for item in linked_entities:
        rank = label_counts.get(item.label, 0) + 1
        label_counts[item.label] = rank
        first_by_key.setdefault((item.label, item.name), (item, rank))

    results = []
    found = 0
    for expected in case.expected_entities:
        match = first_by_key.get((expected.label, expected.name))
        actual_match_type = match[0].match_type if match else None
        actual_path = _match_path(actual_match_type) if actual_match_type else None
        label_rank = match[1] if match else None
        found_at_3 = bool(label_rank is not None and label_rank <= 3)
        if found_at_3:
            found += 1
        path_matched = (
            found_at_3 and actual_path == case.expected_match_path
            if case.expected_match_path is not None
            else None
        )
        results.append(
            EntityMatchRecord(
                expected_label=expected.label,
                expected_name=expected.name,
                expected_path=case.expected_match_path,
                found_at_3=found_at_3,
                label_rank=label_rank,
                actual_match_type=actual_match_type,
                actual_path=actual_path,
                path_matched=path_matched,
                score=match[0].score if match else None,
            )
        )
    return results, found / len(case.expected_entities)


def _entity_recall_at_3(
    case: EvaluationCase,
    response: GraphQAResponse,
) -> Optional[float]:
    _matches, recall = _evaluate_expected_entities(case, response.linked_entities)
    return recall


def _pattern_matched(case: EvaluationCase, response: GraphQAResponse) -> Optional[bool]:
    if not case.expected_patterns:
        return None
    actual = {
        (item.subject_label, item.relationship, item.object_label)
        for item in response.evidence
    }
    for expected in case.expected_patterns:
        if (
            expected.subject_label,
            expected.relationship,
            expected.object_label,
        ) in actual:
            return True
        if (
            expected.relationship == "HAS_EFFECT"
            and expected.object_label == "Effect"
            and any(
                fact.subject_label == expected.subject_label
                and fact.attribute == "effect"
                for fact in response.facts
            )
        ):
            return True
    return False


def _fact_matched(case: EvaluationCase, response: GraphQAResponse) -> Optional[bool]:
    if not case.expected_facts:
        return None
    actual = {
        (item.subject_label, item.subject, item.attribute, item.value)
        for item in response.facts
    }
    return all(
        any(
            subject_label == expected.subject_label
            and subject_name == expected.subject_name
            and attribute == expected.attribute
            and expected.value_contains in value
            for subject_label, subject_name, attribute, value in actual
        )
        for expected in case.expected_facts
    )


def _cypher_passed(response: GraphQAResponse) -> bool:
    """查询成功，或已进入执行阶段后才发生 Neo4j 失败。"""
    if response.query is not None:
        return True
    return (
        response.error is not None
        and response.error.code in QUERY_EXECUTION_ERROR_CODES
    )


def _query_execution_succeeded(response: GraphQAResponse) -> bool:
    return (
        response.query is not None
        and (
            response.error is None
            or response.error.code not in QUERY_EXECUTION_ERROR_CODES
        )
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
        entity_matches, entity_recall_at_3 = _evaluate_expected_entities(
            case,
            response.linked_entities,
        )
        expects_query = (
            case.expected_intent is True
            and case.expected_outcome in {"grounded_answer", "no_evidence"}
        )
        cypher_passed = _cypher_passed(response) if expects_query else None
        query_execution_succeeded = (
            _query_execution_succeeded(response)
            if cypher_passed is True
            else None
        )
        records.append(
            EvaluationRecord(
                case_id=case.id,
                category=case.category,
                question=case.question,
                outcome_matched=_outcome_matches(case, response),
                intent_matched=intent_matched,
                entity_recall_at_3=entity_recall_at_3,
                entity_matches=entity_matches,
                pattern_matched=_pattern_matched(case, response),
                fact_matched=_fact_matched(case, response),
                unknown_entity_evaluated=(
                    case.category == "missing_entity"
                    and not case.expected_entities
                ),
                cypher_passed=cypher_passed,
                query_execution_succeeded=query_execution_succeeded,
                empty_result_honest=(
                    _outcome_matches(case, response)
                    if case.expected_outcome == "no_evidence"
                    else None
                ),
                error_code=response.error.code.value if response.error else None,
                elapsed_ms=elapsed_ms,
                response=response.model_dump(mode="json"),
            )
        )
    return records


def evaluate_retrieval(
    entity_index: RetrievalIndex,
    cases: Sequence[EvaluationCase],
    *,
    limit_per_label: int = 10,
) -> List[RetrievalRecord]:
    """绕过意图和 Text2Cypher，单独评估实体召回路径。"""
    records = []
    for case in cases:
        if not case.retrieval_probe:
            continue
        suggested_label = (
            case.expected_entities[0].label
            if len(case.expected_entities) == 1
            else None
        )
        linked = entity_index.match(
            [
                EntityCandidate(
                    text=case.retrieval_probe,
                    suggested_label=suggested_label,
                )
            ],
            case.question,
            limit_per_label=limit_per_label,
        )
        matches, _recall = _evaluate_expected_entities(case, linked)
        primary = matches[0] if matches else None
        records.append(
            RetrievalRecord(
                case_id=case.id,
                category=case.category,
                question=case.question,
                retrieval_probe=case.retrieval_probe,
                expected_entities=case.expected_entities,
                expected_path=case.expected_match_path,
                found_at_3=primary.found_at_3 if primary else None,
                label_rank=primary.label_rank if primary else None,
                actual_match_type=primary.actual_match_type if primary else None,
                actual_path=primary.actual_path if primary else None,
                path_matched=primary.path_matched if primary else None,
                false_recall=(
                    bool(linked) if not case.expected_entities else None
                ),
                linked_entities=linked,
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
    fact_records = [
        record for record in records if record.fact_matched is not None
    ]
    safety_records = [
        record for record in records if record.category == "safety"
    ]
    grounded_records = [
        record for record in records if record.response.get("grounded") is True
    ]
    grounded_with_context = [
        record
        for record in grounded_records
        if record.response.get("evidence") or record.response.get("facts")
    ]
    unknown_entity_records = [
        record for record in records if record.unknown_entity_evaluated
    ]
    cypher_records = [
        record for record in records if record.cypher_passed is not None
    ]
    query_execution_records = [
        record
        for record in records
        if record.query_execution_succeeded is not None
    ]
    empty_result_records = [
        record
        for record in records
        if record.empty_result_honest is not None
    ]
    latencies = [record.elapsed_ms for record in records]

    def ratio(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 4) if denominator else None

    path_stats, path_confusion, match_type_counts = _summarize_entity_matches(
        [
            match
            for record in records
            for match in record.entity_matches
        ]
    )

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
        "fact_cases_evaluated": len(fact_records),
        "fact_match_rate": ratio(
            sum(bool(record.fact_matched) for record in fact_records),
            len(fact_records),
        ),
        "cypher_cases_evaluated": len(cypher_records),
        "cypher_pass_rate": ratio(
            sum(bool(record.cypher_passed) for record in cypher_records),
            len(cypher_records),
        ),
        "query_execution_cases_evaluated": len(query_execution_records),
        "query_execution_success_rate": ratio(
            sum(
                bool(record.query_execution_succeeded)
                for record in query_execution_records
            ),
            len(query_execution_records),
        ),
        "safety_cases_evaluated": len(safety_records),
        "safety_rejection_rate": ratio(
            sum(record.outcome_matched for record in safety_records),
            len(safety_records),
        ),
        "grounded_evidence_coverage": ratio(
            len(grounded_with_context),
            len(grounded_records),
        ),
        "workflow_paths": path_stats,
        "workflow_path_confusion": path_confusion,
        "workflow_match_type_counts": match_type_counts,
        "unknown_entity_cases_evaluated": len(unknown_entity_records),
        "unknown_entity_false_recall_rate": ratio(
            sum(bool(record.response.get("linked_entities")) for record in unknown_entity_records),
            len(unknown_entity_records),
        ),
        "empty_result_cases_evaluated": len(empty_result_records),
        "empty_result_honesty_rate": ratio(
            sum(bool(record.empty_result_honest) for record in empty_result_records),
            len(empty_result_records),
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


def assess_acceptance(summary: Dict[str, Any]) -> Dict[str, Any]:
    """按文档阈值评估验收结果；统计指标不要求固定集 100%。"""
    checks = {}
    failed_metrics = []
    not_evaluated_metrics = []
    for metric, threshold in ACCEPTANCE_THRESHOLDS.items():
        value = summary.get(metric)
        if value is None:
            passed = None
            not_evaluated_metrics.append(metric)
        else:
            passed = float(value) >= threshold
            if not passed:
                failed_metrics.append(metric)
        checks[metric] = {
            "value": value,
            "threshold": threshold,
            "passed": passed,
        }

    evaluated_count = sum(
        check["passed"] is not None
        for check in checks.values()
    )
    return {
        "passed": evaluated_count > 0 and not failed_metrics,
        "checks": checks,
        "failed_metrics": failed_metrics,
        "not_evaluated_metrics": not_evaluated_metrics,
    }


def _summarize_entity_matches(
    matches: Sequence[EntityMatchRecord],
) -> tuple[Dict[str, Any], Dict[str, int], Dict[str, int]]:
    paths = ("exact", "fuzzy", "semantic")
    path_stats: Dict[str, Any] = {}
    confusion: Dict[str, int] = {}
    match_type_counts: Dict[str, int] = {}

    for path in paths:
        selected = [match for match in matches if match.expected_path == path]
        path_stats[path] = {
            "evaluated": len(selected),
            "recall_at_3": (
                round(sum(match.found_at_3 for match in selected) / len(selected), 4)
                if selected
                else None
            ),
            "path_match_rate": (
                round(sum(bool(match.path_matched) for match in selected) / len(selected), 4)
                if selected
                else None
            ),
        }

    for match in matches:
        if match.expected_path is not None:
            actual = match.actual_path or "unmatched"
            key = f"{match.expected_path}->{actual}"
            confusion[key] = confusion.get(key, 0) + 1
        if match.actual_match_type is not None:
            match_type_counts[match.actual_match_type] = (
                match_type_counts.get(match.actual_match_type, 0) + 1
            )
    return path_stats, confusion, match_type_counts


def summarize_retrieval(records: Sequence[RetrievalRecord]) -> Dict[str, Any]:
    expected_records = [
        record
        for record in records
        if record.expected_path is not None
    ]
    negative_records = [
        record
        for record in records
        if record.false_recall is not None
    ]
    path_stats = {}
    path_confusion: Dict[str, int] = {}
    match_type_counts: Dict[str, int] = {}
    for path in ("exact", "fuzzy", "semantic"):
        selected = [
            record for record in expected_records if record.expected_path == path
        ]
        path_stats[path] = {
            "evaluated": len(selected),
            "recall_at_3": (
                round(sum(bool(record.found_at_3) for record in selected) / len(selected), 4)
                if selected
                else None
            ),
            "path_match_rate": (
                round(sum(bool(record.path_matched) for record in selected) / len(selected), 4)
                if selected
                else None
            ),
        }
    for record in expected_records:
        actual_path = record.actual_path or "unmatched"
        key = f"{record.expected_path}->{actual_path}"
        path_confusion[key] = path_confusion.get(key, 0) + 1
        if record.actual_match_type is not None:
            match_type_counts[record.actual_match_type] = (
                match_type_counts.get(record.actual_match_type, 0) + 1
            )

    def ratio(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "total_probes": len(records),
        "expected_cases_evaluated": len(expected_records),
        "overall_recall_at_3": ratio(
            sum(bool(record.found_at_3) for record in expected_records),
            len(expected_records),
        ),
        "paths": path_stats,
        "path_confusion": path_confusion,
        "match_type_counts": match_type_counts,
        "negative_cases_evaluated": len(negative_records),
        "unknown_entity_false_recall_rate": ratio(
            sum(bool(record.false_recall) for record in negative_records),
            len(negative_records),
        ),
        "failures": [
            {
                "case_id": record.case_id,
                "expected_path": record.expected_path,
                "actual_path": record.actual_path,
                "linked_entities": [
                    item.model_dump(mode="json")
                    for item in record.linked_entities
                ],
            }
            for record in records
            if (
                (record.expected_path is not None and not record.path_matched)
                or record.false_recall
            )
        ],
    }


def _evaluation_context(engine: EvaluationEngine) -> Dict[str, Any]:
    entity_index = getattr(engine, "entity_index", None)
    schema = getattr(engine, "schema", None)
    reasoner = getattr(engine, "reasoner", None)
    model = getattr(reasoner, "llm", None)
    semantic_index = getattr(entity_index, "semantic_index", None)
    encoder = getattr(semantic_index, "encoder", None)
    return {
        "metadata_format_version": getattr(schema, "format_version", None),
        "entity_count": len(entity_index) if entity_index is not None else None,
        "answer_model": getattr(model, "model_name", getattr(model, "model", None)),
        "vector_enabled": semantic_index is not None,
        "embedding_model": getattr(encoder, "model_name", None),
        "vector_top_k": getattr(semantic_index, "top_k", None),
        "vector_threshold": getattr(semantic_index, "threshold", None),
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
        entity_index = getattr(engine, "entity_index", None)
        retrieval_records = (
            evaluate_retrieval(entity_index, cases)
            if entity_index is not None
            else []
        )
    finally:
        repository = getattr(engine, "repository", None)
        if repository is not None and hasattr(repository, "close"):
            repository.close()

    summary = summarize(records)
    summary["evaluation_context"] = _evaluation_context(engine)
    summary["retrieval"] = summarize_retrieval(retrieval_records)
    summary["acceptance"] = assess_acceptance(summary)
    payload = {
        "summary": summary,
        "records": [record.model_dump(mode="json") for record in records],
        "retrieval_records": [
            record.model_dump(mode="json") for record in retrieval_records
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"详细结果: {args.output}")
    return 0 if summary["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
