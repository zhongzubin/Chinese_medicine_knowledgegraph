# -*- coding: utf-8 -*-
"""基于确定性证据的回答审核 Agent。"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence

from graph_qa.agents.base import AgentDescriptor
from graph_qa.models import (
    AggregateFact,
    AnswerVerification,
    AttributeFact,
    Evidence,
    PathEvidence,
    append_disclaimer,
)

WHITESPACE_RE = re.compile(r"\s+")
FALLBACK_WARNING = (
    "回答模型输出的内容未能完整对应本轮证据，"
    "已改为证据化回答。"
)


@dataclass(frozen=True)
class AuditDecision:
    answer: str
    verification: AnswerVerification
    warning: Optional[str] = None


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def _fact_is_covered(answer: str, item: AttributeFact) -> bool:
    normalized_value = normalize_text(item.value)
    if len(normalized_value) >= 2 and normalized_value in answer:
        return True
    parts = [
        part.strip()
        for part in re.split(r"[\s，,、；;。]+", normalized_value)
        if len(part.strip()) >= 2
    ]
    return bool(parts) and all(part in answer for part in parts)


def context_coverage_count(
    answer: str,
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
    aggregates: Sequence[AggregateFact] = (),
    paths: Sequence[PathEvidence] = (),
) -> int:
    """统计回答逐项覆盖的可验证事实数量。"""
    normalized_answer = normalize_text(answer)
    matched = 0
    for item in evidence:
        if len(item.object) >= 2 and item.object in normalized_answer:
            matched += 1
    for item in facts:
        if _fact_is_covered(normalized_answer, item):
            matched += 1
    for item in aggregates:
        if (
            item.description in normalized_answer
            or str(item.count) in normalized_answer
        ):
            matched += 1
    for item in paths:
        if all(
            value in normalized_answer
            for value in (item.root, item.intermediate, item.target)
        ):
            matched += 1
    return matched


def answer_supported_by_context(
    answer: str,
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
    aggregates: Sequence[AggregateFact] = (),
    paths: Sequence[PathEvidence] = (),
) -> bool:
    """回答必须覆盖全部本轮可验证事实。"""
    checked_items = (
        len(evidence)
        + len(facts)
        + len(aggregates)
        + len(paths)
    )
    return checked_items > 0 and context_coverage_count(
        answer,
        evidence,
        facts,
        aggregates,
        paths,
    ) == checked_items


def evidence_fallback_answer(evidence: Sequence[Evidence]) -> str:
    lines = ["根据当前图谱中可验证的关系："]
    for item in evidence[:5]:
        source = f"（来源：{item.source}）" if item.source else ""
        lines.append(
            f"- {item.subject} 通过 {item.relationship} "
            f"关联到 {item.object}{source}"
        )
    return "\n".join(lines)


def aggregate_answer(aggregates: Sequence[AggregateFact]) -> str:
    lines = ["根据当前图谱统计："]
    for item in aggregates:
        lines.append(f"- {item.description}共有 {item.count} 个")
    return "\n".join(lines)


def context_fallback_answer(
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
) -> str:
    lines = ["根据当前图谱中可验证的信息："]
    for item in facts[:8]:
        source = f"（来源：{item.source}）" if item.source else "（未提供出处）"
        lines.append(
            f"- {item.subject}的{item.attribute_label}：{item.value}{source}"
        )
    for item in evidence[:8]:
        source = f"（来源：{item.source}）" if item.source else "（未提供出处）"
        lines.append(
            f"- {item.subject} 通过 {item.relationship} 关联到 "
            f"{item.object}{source}"
        )
    return "\n".join(lines)


def path_fallback_answer(paths: Sequence[PathEvidence]) -> str:
    lines = ["根据当前图谱中可验证的路径："]
    for item in paths[:8]:
        source = f"（来源：{item.source}）" if item.source else "（未提供出处）"
        lines.append(
            f"- ({item.root}:{item.root_label})"
            f"-[{item.first_relationship}]->"
            f"({item.intermediate}:{item.intermediate_label})"
            f"-[{item.second_relationship}]->"
            f"({item.target}:{item.target_label}){source}"
        )
    return "\n".join(lines)


def audit_fallback_answer(
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
    aggregates: Sequence[AggregateFact],
    paths: Sequence[PathEvidence],
) -> str:
    parts = []
    if facts or evidence:
        parts.append(context_fallback_answer(evidence, facts))
    if aggregates:
        parts.append(aggregate_answer(aggregates))
    if paths:
        parts.append(path_fallback_answer(paths))
    if not parts:
        parts.append("当前没有可用于生成回答的图谱证据。")
    return append_disclaimer("\n\n".join(parts))


@dataclass
class EvidenceAuditAgent:
    descriptor: AgentDescriptor = AgentDescriptor(
        name="evidence_audit",
        responsibility="确定性核对回答事实、关系证据、聚合结果和路径证据",
        stages=(
            "verify_answer",
            "clarify",
            "no_evidence",
            "safe_fail",
            "finish",
        ),
        tools=(
            "deterministic_support_check",
            "source_coverage",
            "evidence_fallback",
        ),
    )

    def audit(
        self,
        answer: str,
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact],
        aggregates: Sequence[AggregateFact] = (),
        paths: Sequence[PathEvidence] = (),
    ) -> AuditDecision:
        checked_evidence = list(evidence)
        checked_facts = list(facts)
        checked_aggregates = list(aggregates)
        checked_paths = list(paths)
        checked_items = (
            len(checked_evidence)
            + len(checked_facts)
            + len(checked_aggregates)
            + len(checked_paths)
        )
        missing_sources = sum(
            item.source is None
            for item in (
                *checked_evidence,
                *checked_facts,
                *checked_paths,
            )
        )
        normalized_answer = answer.strip()
        matched_items = context_coverage_count(
            normalized_answer,
            checked_evidence,
            checked_facts,
            checked_aggregates,
            checked_paths,
        )
        supported = (
            bool(normalized_answer)
            and checked_items > 0
            and matched_items == checked_items
        )
        if supported:
            verification = AnswerVerification(
                status="passed",
                checked_items=checked_items,
                matched_items=matched_items,
                evidence_items=len(checked_evidence),
                fact_items=len(checked_facts),
                aggregate_items=len(checked_aggregates),
                path_items=len(checked_paths),
                missing_sources=missing_sources,
                fallback_used=False,
                message=(
                    f"已逐项核对 {checked_items} 项图谱证据，"
                    "回答覆盖全部可验证事实。"
                ),
            )
            return AuditDecision(
                answer=normalized_answer,
                verification=verification,
            )

        fallback = audit_fallback_answer(
            checked_evidence,
            checked_facts,
            checked_aggregates,
            checked_paths,
        )
        verification = AnswerVerification(
            status="fallback",
            checked_items=checked_items,
            matched_items=matched_items,
            evidence_items=len(checked_evidence),
            fact_items=len(checked_facts),
            aggregate_items=len(checked_aggregates),
            path_items=len(checked_paths),
            missing_sources=missing_sources,
            fallback_used=True,
            message=(
                f"原回答覆盖 {matched_items}/{checked_items} 项图谱证据，"
                "已回退为证据化回答。"
            ),
        )
        return AuditDecision(
            answer=fallback,
            verification=verification,
            warning=FALLBACK_WARNING,
        )
