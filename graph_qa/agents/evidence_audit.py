# -*- coding: utf-8 -*-
"""基于确定性证据的回答审核 Agent。"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from graph_qa.agents.base import AgentDescriptor
from graph_qa.answer_support import support_items_by_id
from graph_qa.models import (
    AggregateFact,
    AnswerVerification,
    AttributeFact,
    Evidence,
    GraphAnswerDraft,
    PathEvidence,
    append_disclaimer,
)

WHITESPACE_RE = re.compile(r"\s+")
NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?"
    r"(?:%|克|毫克|千克|公斤|斤|两|钱|毫升|升|片|枚|粒|次|日|天|周|个月)?"
)
BOOK_TITLE_RE = re.compile(r"《[^》]+》")
NEGATION_MARKERS = ("不", "未", "无", "没有", "并非", "不能", "禁止")
FALLBACK_WARNING = (
    "回答模型输出的内容未能完整对应本轮证据，"
    "已改为证据化回答。"
)
SOURCE_NOTICE = "来源说明：当前图谱未提供出处。"
RELATION_ANSWER_TEMPLATES = {
    "HAS_EFFECT": "{subject}具有以下功效：{objects}",
    "HAS_INGREDIENT": "{subject}含有以下成分：{objects}",
    "TREATS_DISEASE": "{subject}可用于治疗：{objects}",
    "ALLEVIATES_SYMPTOM": "{subject}可缓解：{objects}",
    "HAS_SYMPTOM": "{subject}可能涉及以下症状：{objects}",
    "FROM_SOURCE": "{subject}的相关出处：{objects}",
}


@dataclass(frozen=True)
class AuditDecision:
    answer: str
    verification: AnswerVerification
    warning: Optional[str] = None


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def _fact_is_covered(answer: str, item: AttributeFact) -> bool:
    if normalize_text(item.subject) not in answer:
        return False
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
        if (
            normalize_text(item.subject) in normalized_answer
            and len(item.object) >= 2
            and normalize_text(item.object) in normalized_answer
        ):
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
            for value in (item.root, *(step.object for step in item.steps))
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


def _support_text(item: Evidence | AttributeFact) -> str:
    values = item.model_dump(mode="json").values()
    return normalize_text(
        " ".join(str(value) for value in values if value is not None)
    )


def _claim_matches_support(text: str, item: Evidence | AttributeFact) -> bool:
    normalized_text = normalize_text(text)
    subject = normalize_text(item.subject)
    if subject not in normalized_text:
        return False

    if isinstance(item, Evidence):
        object_value = normalize_text(item.object)
        if not object_value or object_value not in normalized_text:
            return False
        if normalized_text.index(subject) >= normalized_text.index(object_value):
            return False
    else:
        values = [
            normalize_text(part)
            for part in re.split(r"[\s，,、；;。]+", item.value)
            if len(part.strip()) >= 2
        ]
        if not values or not all(value in normalized_text for value in values):
            return False
        if normalized_text.index(subject) >= normalized_text.index(values[0]):
            return False

    support_text = _support_text(item)
    for marker in NEGATION_MARKERS:
        if marker in normalized_text and marker not in support_text:
            return False
    for number in NUMBER_RE.findall(normalized_text):
        if normalize_text(number) not in support_text:
            return False
    for title in BOOK_TITLE_RE.findall(normalized_text):
        if normalize_text(title) not in support_text:
            return False
    return True


def validate_answer_draft(
    draft: GraphAnswerDraft,
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
) -> list[str]:
    """核对结构化回答的证据编号、覆盖范围和 claim 文本。"""
    support = support_items_by_id(evidence, facts)
    covered = set()
    errors = []
    for claim_index, claim in enumerate(draft.claims, start=1):
        for support_id in claim.support_ids:
            item = support.get(support_id)
            if item is None:
                errors.append(f"C{claim_index} 引用了未知证据编号 {support_id}")
                continue
            covered.add(support_id)
            if not _claim_matches_support(claim.text, item):
                errors.append(f"C{claim_index} 与证据 {support_id} 不匹配")

    missing = sorted(set(support) - covered)
    if missing:
        errors.append(f"未覆盖证据编号: {', '.join(missing)}")
    return errors


def ensure_source_disclosure(
    answer: str,
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
    paths: Sequence[PathEvidence],
) -> str:
    """存在缺失来源时，由程序统一补充来源状态。"""
    has_missing_source = any(
        item.source is None
        for item in (*evidence, *facts, *paths)
    )
    if has_missing_source and "未提供出处" not in answer:
        return f"{answer.rstrip()}\n\n{SOURCE_NOTICE}"
    return answer


def supports_controlled_evidence_answer(evidence: Sequence[Evidence]) -> bool:
    return bool(evidence) and all(
        item.relationship in RELATION_ANSWER_TEMPLATES
        for item in evidence
    )


def evidence_fallback_answer(evidence: Sequence[Evidence]) -> str:
    grouped: Dict[tuple[str, str, str], list[Evidence]] = {}
    for item in evidence:
        key = (item.subject, item.subject_label, item.relationship)
        grouped.setdefault(key, []).append(item)

    lines = ["根据当前图谱中可验证的信息："]
    for (subject, _label, relationship), items in grouped.items():
        objects = list(dict.fromkeys(item.object for item in items))
        template = RELATION_ANSWER_TEMPLATES.get(
            relationship,
            "{subject}通过 " + relationship + " 关联到：{objects}",
        )
        sentence = template.format(
            subject=subject,
            objects="、".join(objects),
        )
        sources = list(
            dict.fromkeys(
                item.source for item in items if item.source
            )
        )
        source_text = f"（来源：{'；'.join(sources)}）" if sources else ""
        lines.append(f"- {sentence}{source_text}")
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
        node_text = item.root
        for step in item.steps:
            node_text += (
                f"-[{step.relationship}]->{step.object}"
            )
        lines.append(
            f"- {node_text}{source}"
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
        answer_draft: Optional[GraphAnswerDraft] = None,
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
        validation_errors = (
            validate_answer_draft(answer_draft, checked_evidence, checked_facts)
            if answer_draft is not None
            else []
        )
        if answer_draft is not None:
            normalized_answer = "\n".join(
                f"- {claim.text}" for claim in answer_draft.claims
            )
        else:
            normalized_answer = answer.strip()
        normalized_answer = ensure_source_disclosure(
            normalized_answer,
            checked_evidence,
            checked_facts,
            checked_paths,
        )
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
            and not validation_errors
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
        fallback = ensure_source_disclosure(
            fallback,
            checked_evidence,
            checked_facts,
            checked_paths,
        )
        if validation_errors:
            fallback_message = (
                f"原回答有 {len(validation_errors)} 项断言未通过证据编号核验，"
                "已回退为证据化回答。"
            )
        else:
            fallback_message = (
                f"原回答覆盖 {matched_items}/{checked_items} 项图谱证据，"
                "已回退为证据化回答。"
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
            message=fallback_message,
        )
        return AuditDecision(
            answer=fallback,
            verification=verification,
            warning=FALLBACK_WARNING,
        )
