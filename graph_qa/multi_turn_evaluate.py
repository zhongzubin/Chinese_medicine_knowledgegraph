# -*- coding: utf-8 -*-
"""多轮上下文固定集评测入口。"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from graph_qa.cli import CLIConfigError, build_engine_from_environment
from graph_qa.models import ConversationTurn, ErrorCode, GraphQAResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = PROJECT_ROOT / "evaluation" / "multi_turn_cases.jsonl"
ACCEPTANCE_THRESHOLDS = {
    "outcome_accuracy": 0.90,
    "entity_match_rate": 0.90,
}


class MultiTurnModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedEntity(MultiTurnModel):
    label: str
    name: str


class MultiTurnStep(MultiTurnModel):
    question: str
    expected_entities: List[ExpectedEntity] = Field(default_factory=list)
    expected_outcome: Literal[
        "grounded_answer",
        "direct_answer",
        "clarification",
        "no_evidence",
        "safe_rejection",
    ]


class MultiTurnCase(MultiTurnModel):
    id: str
    turns: List[MultiTurnStep] = Field(min_length=2, max_length=5)


class MultiTurnStepRecord(MultiTurnModel):
    turn_index: int
    question: str
    expected_outcome: str
    outcome_matched: bool
    entity_found: Optional[bool]
    entity_rank: Optional[int]
    error_code: Optional[str]
    response: Dict[str, Any]


class MultiTurnCaseRecord(MultiTurnModel):
    case_id: str
    steps: List[MultiTurnStepRecord]


class MultiTurnEngine(Protocol):
    def answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[list[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> GraphQAResponse:
        ...


def load_cases(path: Path = DEFAULT_CASES_PATH) -> List[MultiTurnCase]:
    if not path.exists():
        raise ValueError(f"未找到多轮评测集: {path}")
    cases = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                cases.append(MultiTurnCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(
                    f"{path}:{line_number} 多轮用例格式错误"
                ) from exc
    if not cases:
        raise ValueError("多轮评测集为空")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("多轮评测集存在重复 id")
    return cases


def _outcome_matched(step: MultiTurnStep, response: GraphQAResponse) -> bool:
    error_code = response.error.code if response.error else None
    if step.expected_outcome == "grounded_answer":
        return (
            error_code is None
            and response.grounded
            and bool(response.evidence or response.facts)
        )
    if step.expected_outcome == "direct_answer":
        return error_code is None and not response.grounded
    if step.expected_outcome == "clarification":
        return (
            error_code is None
            and not response.grounded
            and "补充" in response.answer
        )
    if step.expected_outcome == "no_evidence":
        return (
            error_code is None
            and not response.grounded
            and "没有找到" in response.answer
        )
    if step.expected_outcome == "safe_rejection":
        return error_code in {
            ErrorCode.UNSAFE_REQUEST,
            ErrorCode.CYPHER_REJECTED,
        }
    return False


def _expected_entity_rank(
    step: MultiTurnStep,
    response: GraphQAResponse,
) -> tuple[Optional[bool], Optional[int]]:
    if not step.expected_entities:
        return None, None
    expected = {
        (item.label, item.name)
        for item in step.expected_entities
    }
    label_counts: Dict[str, int] = {}
    for entity in response.linked_entities:
        rank = label_counts.get(entity.label, 0) + 1
        label_counts[entity.label] = rank
        if (entity.label, entity.name) in expected:
            return True, rank
    return False, None


def evaluate_cases(
    engine: MultiTurnEngine,
    cases: Sequence[MultiTurnCase],
) -> List[MultiTurnCaseRecord]:
    records = []
    for case in cases:
        history: List[ConversationTurn] = []
        steps = []
        for turn_index, step in enumerate(case.turns, start=1):
            response = engine.answer(
                step.question,
                request_id=f"{case.id}-{turn_index}",
                conversation_history=history,
                session_id=case.id,
            )
            found, rank = _expected_entity_rank(step, response)
            steps.append(
                MultiTurnStepRecord(
                    turn_index=turn_index,
                    question=step.question,
                    expected_outcome=step.expected_outcome,
                    outcome_matched=_outcome_matched(step, response),
                    entity_found=found,
                    entity_rank=rank,
                    error_code=(
                        response.error.code.value
                        if response.error is not None
                        else None
                    ),
                    response=response.model_dump(mode="json"),
                )
            )
            if response.error is None:
                history.append(
                    ConversationTurn(
                        question=step.question,
                        answer=response.answer,
                        linked_entities=response.linked_entities,
                        grounded=response.grounded,
                    )
                )
        records.append(MultiTurnCaseRecord(case_id=case.id, steps=steps))
    return records


def summarize(records: Sequence[MultiTurnCaseRecord]) -> Dict[str, Any]:
    steps = [step for record in records for step in record.steps]
    entity_steps = [step for step in steps if step.entity_found is not None]
    failed_cases = [
        record
        for record in records
        if not all(step.outcome_matched for step in record.steps)
    ]

    def ratio(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 4) if denominator else None

    summary = {
        "cases": len(records),
        "turns": len(steps),
        "outcome_accuracy": ratio(
            sum(step.outcome_matched for step in steps),
            len(steps),
        ),
        "entity_checks": len(entity_steps),
        "entity_match_rate": ratio(
            sum(bool(step.entity_found) for step in entity_steps),
            len(entity_steps),
        ),
        "failed_cases": [
            {
                "case_id": record.case_id,
                "failed_turns": [
                    step.turn_index
                    for step in record.steps
                    if not step.outcome_matched
                ],
            }
            for record in failed_cases
        ],
    }
    checks = {}
    failed_metrics = []
    for metric, threshold in ACCEPTANCE_THRESHOLDS.items():
        value = summary.get(metric)
        passed = value is not None and value >= threshold
        if value is not None and not passed:
            failed_metrics.append(metric)
        checks[metric] = {
            "value": value,
            "threshold": threshold,
            "passed": passed if value is not None else None,
        }
    summary["acceptance"] = {
        "passed": bool(checks) and not failed_metrics and all(
            check["passed"] is True
            for check in checks.values()
        ),
        "checks": checks,
        "failed_metrics": failed_metrics,
    }
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行中医图谱多轮上下文评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="只运行指定用例，可重复传入",
    )
    parser.add_argument("--database")
    parser.add_argument("--max-results", type=int, default=50)
    args = parser.parse_args()
    if args.max_results < 1:
        parser.error("--max-results 必须大于等于 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        cases = load_cases(args.cases)
        if args.case_id:
            selected = set(args.case_id)
            cases = [case for case in cases if case.id in selected]
            if not cases:
                raise ValueError("指定的多轮用例不存在")
        engine = build_engine_from_environment(
            args.database,
            args.max_results,
        )
    except (ValueError, CLIConfigError) as exc:
        print(f"多轮评测初始化失败: {exc}")
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
    return 0 if summary["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
