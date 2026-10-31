# -*- coding: utf-8 -*-
"""可验证的中医图谱问答 LangGraph 工作流。"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from graph_qa.agents import QAAgentTeam
from graph_qa.cypher_guard import CypherGuard, CypherGuardError, detect_unsafe_question
from graph_qa.entity_linker import EntityIndex
from graph_qa.llm import QAReasoner, ReasonerError
from graph_qa.models import (
    AggregateFact,
    AggregateQueryPlan,
    AttributeFact,
    AttributeQueryPlan,
    AttributeSelection,
    ConversationTurn,
    CypherPlan,
    ErrorCode,
    Evidence,
    GraphQAStreamEvent,
    GraphQARequest,
    GraphQAResponse,
    IntentResult,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    MultiHopStep,
    PathEvidence,
    QueryInfo,
    append_disclaimer,
)
from graph_qa.neo4j_repository import RepositoryError
from graph_qa.schema import ATTRIBUTE_DISPLAY_NAMES, GraphSchema
from graph_qa.state import AgentState

WHITESPACE_RE = re.compile(r"\s+")

NO_EVIDENCE_ANSWER = "当前图谱没有找到足以回答该问题的依据。"
CLARIFY_ANSWER = (
    "我暂时没有在知识图谱中匹配到具体实体。"
    "请补充药材、方剂、疾病、症状、功效或文献名称后再试。"
)
SAFE_RETRY_ANSWER = "未能生成符合只读安全规则的图谱查询，请换一种更具体的问法。"

ATTRIBUTE_TERM_MAP: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...] = (
    (("性味", "四气", "五味", "药性", "寒热温凉"), ("property_flavor",)),
    (("归经", "入哪经", "经络"), ("meridian",)),
    (("用量", "剂量", "多少克", "吃多少", "服多少"), ("dosage",)),
    (("禁忌", "忌服", "不宜", "注意事项", "不能吃", "不能服"), ("taboo",)),
    (
        (
            "用法",
            "怎么用",
            "如何服",
            "怎么服",
            "如何服用",
            "怎么服用",
            "煎服",
            "服用方法",
            "怎么吃",
        ),
        ("usage",),
    ),
    (("功效", "作用", "有什么效果"), ("effect",)),
    (("主治", "治什么", "治疗什么", "适应证"), ("indication",)),
    (("产地", "哪里产"), ("place",)),
    (("炮制", "怎么炮制"), ("processing",)),
    (("性状", "外观"), ("traits",)),
    (("别名", "又叫什么", "别称"), ("alias",)),
)
RELATION_FALLBACK_ATTRIBUTES = frozenset({"effect", "indication"})
RELATION_TERM_MAP: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (
        ("组成", "成分", "含有", "所含", "包含", "由哪些药材"),
        "HAS_INGREDIENT",
    ),
    (("缓解", "改善"), "ALLEVIATES_SYMPTOM"),
    (("治疗", "主治", "治什么", "能治"), "TREATS_DISEASE"),
    (("功效", "作用", "效果"), "HAS_EFFECT"),
    (("症状", "体征", "表现"), "HAS_SYMPTOM"),
    (("出处", "来源", "文献", "出自", "来自"), "FROM_SOURCE"),
)
ENTITY_HINT_MAP: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("方剂",), "Formula"),
    (("药材", "中药"), "Herb"),
    (("疾病", "病证"), "Disease"),
    (("症状", "体征"), "Symptom"),
    (("功效", "作用"), "Effect"),
    (("文献", "出处", "来源", "典籍"), "Source"),
)
CONTEXT_PRONOUNS = (
    "这个方剂",
    "该方剂",
    "这个药",
    "这种药",
    "该药",
    "这个病",
    "这种病",
    "这个症状",
    "该症状",
    "它",
)
COUNT_TERMS = ("多少", "几个", "几种", "数量", "多少个", "多少种")
ENTITY_LABEL_DISPLAY_NAMES = {
    "Herb": "药材",
    "Formula": "方剂",
    "Disease": "疾病",
    "Symptom": "症状",
    "Effect": "功效",
    "Source": "文献",
}
MULTI_HOP_CATALOG = (
    (
        ("Formula", "HAS_INGREDIENT", "Herb"),
        ("Herb", "HAS_EFFECT", "Effect"),
        "所含药材的功效",
    ),
    (
        ("Formula", "HAS_INGREDIENT", "Herb"),
        ("Herb", "TREATS_DISEASE", "Disease"),
        "所含药材治疗的疾病",
    ),
    (
        ("Formula", "HAS_INGREDIENT", "Herb"),
        ("Herb", "ALLEVIATES_SYMPTOM", "Symptom"),
        "所含药材缓解的症状",
    ),
    (
        ("Herb", "TREATS_DISEASE", "Disease"),
        ("Disease", "HAS_SYMPTOM", "Symptom"),
        "治疗疾病的相关症状",
    ),
)


@dataclass(frozen=True)
class RelationshipSelection:
    subject_label: str
    relationship: str
    object_label: str
    direction: str
    entities: Tuple[Any, ...]

STAGE_LABELS = {
    "normalize": "规范化问题",
    "rewrite_question": "结合历史改写问题",
    "prelink_entities": "预匹配图谱实体",
    "classify_intent": "识别问答场景",
    "extract_entities": "抽取实体候选",
    "link_entities": "匹配图谱实体",
    "plan_cypher": "生成图谱查询",
    "validate_cypher": "校验查询安全",
    "execute_cypher": "执行图谱查询",
    "answer_from_graph": "生成图谱回答",
    "verify_answer": "校验答案",
    "direct_answer": "生成直接回答",
    "clarify": "请求补充实体",
    "no_evidence": "确认无图谱依据",
    "safe_fail": "安全失败",
    "finish": "完成处理",
}


def normalize_question(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def evidence_from_rows(
    rows: Sequence[dict],
    schema: GraphSchema,
) -> List[Evidence]:
    """只把固定证据字段转换为 Evidence，无法验证的行直接丢弃。"""
    evidence_by_key: Dict[Tuple[str, str, str, str, str], Evidence] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = row.get("subject")
        subject_label = row.get("subject_label")
        relationship = row.get("relationship")
        object_name = row.get("object")
        object_label = row.get("object_label")
        source = row.get("source")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (subject, subject_label, relationship, object_name, object_label)
        ):
            continue
        if subject_label not in schema.labels or object_label not in schema.labels:
            continue
        if relationship not in schema.relationship_types:
            continue
        if (subject_label, relationship, object_label) not in schema.triple_patterns:
            continue
        normalized_source = source.strip() if isinstance(source, str) and source.strip() else None
        key = (
            subject,
            subject_label,
            relationship,
            object_name,
            object_label,
        )
        existing = evidence_by_key.get(key)
        if existing is None:
            evidence_by_key[key] = Evidence(
                subject=subject.strip(),
                subject_label=subject_label,
                relationship=relationship,
                object=object_name.strip(),
                object_label=object_label,
                source=normalized_source,
            )
            continue
        if normalized_source is None:
            continue
        existing_sources = (
            existing.source.split("；")
            if existing.source
            else []
        )
        if normalized_source not in existing_sources:
            existing_sources.append(normalized_source)
            evidence_by_key[key] = existing.model_copy(
                update={"source": "；".join(existing_sources)}
            )
    return list(evidence_by_key.values())


def _evidence_fallback_answer(evidence: Sequence[Evidence]) -> str:
    lines = ["根据当前图谱中可验证的关系："]
    for item in evidence[:5]:
        source = f"（来源：{item.source}）" if item.source else ""
        lines.append(
            f"- {item.subject} 通过 {item.relationship} 关联到 {item.object}{source}"
        )
    return "\n".join(lines)


def _evidence_entity_keys(evidence: Evidence) -> Set[Tuple[str, str]]:
    return {
        (evidence.subject_label, evidence.subject),
        (evidence.object_label, evidence.object),
    }


def _relevant_evidence(
    evidence: Sequence[Evidence],
    used_entities: Sequence[Any],
) -> List[Evidence]:
    used_keys = {(item.label, item.name) for item in used_entities}
    if not used_keys:
        return []
    return [
        item
        for item in evidence
        if bool(_evidence_entity_keys(item) & used_keys)
    ]


def evidence_matches_used_entities(
    evidence: Sequence[Evidence],
    used_entities: Sequence[Any],
) -> bool:
    """至少一条可验证证据必须关联本轮查询目标。"""
    return bool(_relevant_evidence(evidence, used_entities))


def _parameter_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _active_question(state: AgentState) -> str:
    return state.get("rewritten_question") or state["normalized_question"]


def _requested_attributes(question: str) -> List[str]:
    requested: List[str] = []
    for terms, attributes in ATTRIBUTE_TERM_MAP:
        if not any(term in question for term in terms):
            continue
        for attribute in attributes:
            if attribute not in requested:
                requested.append(attribute)
    return requested


def _looks_context_dependent(question: str) -> bool:
    if any(pronoun in question for pronoun in CONTEXT_PRONOUNS):
        return True
    if (
        _requested_attributes(question)
        and len(question) <= 20
        and not any(term in question for term in ("吗", "什么", "是不是"))
    ):
        return True
    return question in {"还有呢？", "还有呢?", "为什么？", "为什么?"}


def _requested_relations(question: str) -> Set[str]:
    return {
        relation
        for terms, relation in RELATION_TERM_MAP
        if any(term in question for term in terms)
    }


def _entity_hints(question: str) -> Set[str]:
    return {
        label
        for terms, label in ENTITY_HINT_MAP
        if any(term in question for term in terms)
    }


def _is_count_question(question: str) -> bool:
    return any(term in question for term in COUNT_TERMS)


def _has_explicit_conjunction(question: str) -> bool:
    return any(
        marker in question
        for marker in ("同时", "既", "都", "并且", "和", "与", "、")
    )


def _aggregate_description(
    anchor_names: Sequence[str],
    result_label: str,
    direction: str,
) -> str:
    anchor_text = "、".join(anchor_names)
    result_text = ENTITY_LABEL_DISPLAY_NAMES.get(result_label, result_label)
    if direction == "incoming" and len(anchor_names) > 1:
        return f"同时关联到{anchor_text}的{result_text}"
    if direction == "incoming":
        return f"关联到{anchor_text}的{result_text}"
    return f"{anchor_text}关联的{result_text}"


def _relationship_direction_hint(
    question: str,
    linked_entities: Sequence[Any],
    relation: str,
) -> Optional[str]:
    relation_positions = [
        question.find(term)
        for terms, requested_relation in RELATION_TERM_MAP
        if requested_relation == relation
        for term in terms
        if term in question
    ]
    entity_positions = [
        question.find(token)
        for entity in linked_entities
        for token in (entity.matched_text, entity.name)
        if token and token in question
    ]
    if not relation_positions or not entity_positions:
        return None
    return (
        "forward"
        if min(entity_positions) < min(relation_positions)
        else "reverse"
    )


def _fact_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bool, int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [_fact_value_text(item) for item in value]
        return "、".join(part for part in parts if part)
    if isinstance(value, Mapping):
        parts = [
            f"{key}: {_fact_value_text(item)}"
            for key, item in value.items()
            if _fact_value_text(item)
        ]
        return "；".join(parts)
    return str(value).strip()


def facts_from_rows(
    rows: Sequence[dict],
    plan: AttributeQueryPlan,
) -> List[AttributeFact]:
    selections = {
        selection.label: set(selection.attributes)
        for selection in plan.selections
    }
    used_keys = {(item.label, item.name) for item in plan.used_entities}
    facts: List[AttributeFact] = []
    seen: Set[Tuple[str, str, str, str, Optional[str]]] = set()
    for row in rows:
        subject = row.get("subject")
        subject_label = row.get("subject_label")
        properties = row.get("properties")
        if (
            not isinstance(subject, str)
            or not isinstance(subject_label, str)
            or not isinstance(properties, Mapping)
            or (subject_label, subject) not in used_keys
            or subject_label not in selections
        ):
            continue
        sources = row.get("sources")
        source = None
        if isinstance(sources, list):
            source_names = [
                item.strip()
                for item in sources
                if isinstance(item, str) and item.strip()
            ]
            source = "；".join(source_names) or None
        for attribute in selections[subject_label]:
            value = _fact_value_text(properties.get(attribute))[:4000]
            if not value:
                continue
            key = (subject, subject_label, attribute, value, source)
            if key in seen:
                continue
            seen.add(key)
            facts.append(
                AttributeFact(
                    subject=subject,
                    subject_label=subject_label,
                    attribute=attribute,
                    attribute_label=ATTRIBUTE_DISPLAY_NAMES.get(
                        attribute,
                        attribute,
                    ),
                    value=value,
                    source=source,
                )
            )
    return facts


def _answer_supported_by_context(
    answer: str,
    evidence: Sequence[Evidence],
    facts: Sequence[AttributeFact],
    aggregates: Sequence[AggregateFact] = (),
    paths: Sequence[PathEvidence] = (),
) -> bool:
    normalized_answer = normalize_question(answer)
    candidates: Set[str] = set()
    for item in evidence:
        candidates.add(item.object)
    for item in facts:
        candidates.add(item.value)
        candidates.update(
            part.strip()
            for part in re.split(r"[\s，,、；;。]+", item.value)
            if part.strip()
        )
    for item in aggregates:
        candidates.add(str(item.count))
        candidates.add(item.description)
    for item in paths:
        candidates.add(item.intermediate)
        candidates.add(item.target)
    return any(
        len(candidate) >= 2 and candidate in normalized_answer
        for candidate in candidates
    )


def _aggregate_answer(aggregates: Sequence[AggregateFact]) -> str:
    lines = ["根据当前图谱统计："]
    for item in aggregates:
        lines.append(f"- {item.description}共有 {item.count} 个")
    return "\n".join(lines)


def _multi_hop_answer(
    plan: MultiHopQueryPlan,
    paths: Sequence[PathEvidence],
) -> str:
    grouped: Dict[str, List[str]] = {}
    root_by_intermediate: Dict[str, str] = {}
    second_relationship = plan.steps[1].relationship
    for item in paths:
        values = grouped.setdefault(item.intermediate, [])
        if item.target not in values:
            values.append(item.target)
        root_by_intermediate[item.intermediate] = item.root
    lines = [f"根据当前图谱，{plan.description}如下："]
    for intermediate, targets in grouped.items():
        root = root_by_intermediate[intermediate]
        prefix = f"{root} → " if root != intermediate else ""
        if second_relationship == "HAS_EFFECT":
            value = "、".join(targets)
        elif second_relationship == "TREATS_DISEASE":
            value = "可治疗" + "、".join(targets)
        elif second_relationship == "ALLEVIATES_SYMPTOM":
            value = "可缓解" + "、".join(targets)
        else:
            value = "、".join(targets)
        lines.append(f"- {prefix}{intermediate}：{value}")
    return "\n".join(lines)


def facts_match_used_entities(
    facts: Sequence[AttributeFact],
    used_entities: Sequence[Any],
) -> bool:
    used_keys = {(item.label, item.name) for item in used_entities}
    return bool(facts) and all(
        (item.subject_label, item.subject) in used_keys
        for item in facts
    )


def _context_fallback_answer(
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


def _normalize_answer_formatting(
    answer: str,
    facts: Sequence[AttributeFact] = (),
) -> str:
    """修复模型把 Markdown 项目符号挤在句末的可读性问题。"""
    normalized = re.sub(
        r"(?<=[：:。；;])\s*-\s*(?=\S)",
        "\n- ",
        answer,
    )
    for label in {
        item.attribute_label
        for item in facts
        if item.attribute_label
    }:
        escaped = re.escape(label)
        normalized = re.sub(
            rf"[-•]\s*(?:\*\*)?{escaped}(?:\*\*)?\s*[：:]",
            f"\n{label}：",
            normalized,
        )
        normalized = re.sub(
            rf"(?<=[。；;])\s*(?:\*\*)?{escaped}(?:\*\*)?\s*[：:]",
            f"\n{label}：",
            normalized,
        )
        normalized = re.sub(
            rf"\n\n(?={escaped}：)",
            "\n",
            normalized,
        )
    return normalized.strip()


def _recent_contextual_question(
    question: str,
    history: Sequence[ConversationTurn],
) -> Optional[str]:
    if not history:
        return None
    recent_entities = next(
        (
            turn.linked_entities
            for turn in reversed(history)
            if turn.linked_entities
        ),
        [],
    )
    if not recent_entities:
        return None

    preferred_label = None
    pronoun = None
    for candidate, label in (
        ("这个方剂", "Formula"),
        ("该方剂", "Formula"),
        ("这个药", "Herb"),
        ("这种药", "Herb"),
        ("该药", "Herb"),
        ("这个病", "Disease"),
        ("这种病", "Disease"),
        ("这个症状", "Symptom"),
        ("该症状", "Symptom"),
        ("它", None),
    ):
        if candidate in question:
            pronoun = candidate
            preferred_label = label
            break

    if pronoun is None and _requested_attributes(question) and len(question) <= 20:
        pronoun = ""

    if pronoun is None:
        return None

    candidates = [
        item
        for item in recent_entities
        if preferred_label is None or item.label == preferred_label
    ]
    unique_names = {item.name for item in candidates}
    if len(unique_names) != 1:
        return None
    entity_name = next(iter(unique_names))
    resolved = question.replace(pronoun, entity_name, 1) if pronoun else (
        f"{entity_name}{question}"
    )
    return normalize_question(resolved)


def _emit_stream_event(event: str, data: Dict[str, Any]) -> None:
    try:
        get_stream_writer()({"event": event, "data": data})
    except Exception:
        return


def _emit_answer_delta(text: str) -> None:
    if text:
        _emit_stream_event("answer_delta", {"delta": text})


class GraphQAEngine:
    def __init__(
        self,
        reasoner: QAReasoner,
        repository: Any,
        schema: GraphSchema,
        entity_index: Optional[EntityIndex] = None,
        max_attempts: int = 3,
        entity_limit: int = 10,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于等于 1")
        if entity_limit < 1:
            raise ValueError("entity_limit 必须大于等于 1")
        self.reasoner = reasoner
        self.agents = QAAgentTeam(reasoner)
        self.repository = repository
        self.schema = schema
        self.entity_index = entity_index or repository.load_entity_index()
        self.max_attempts = max_attempts
        self.entity_limit = entity_limit
        self.guard = CypherGuard(
            schema,
            max_results=getattr(repository, "max_results", 50),
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("normalize", self._normalize)
        builder.add_node("rewrite_question", self._rewrite_question)
        builder.add_node("prelink_entities", self._prelink_entities)
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("extract_entities", self._extract_entities)
        builder.add_node("link_entities", self._link_entities)
        builder.add_node("plan_cypher", self._plan_cypher)
        builder.add_node("validate_cypher", self._validate_cypher)
        builder.add_node("execute_cypher", self._execute_cypher)
        builder.add_node("answer_from_graph", self._answer_from_graph)
        builder.add_node("verify_answer", self._verify_answer)
        builder.add_node("direct_answer", self._direct_answer)
        builder.add_node("clarify", self._clarify)
        builder.add_node("no_evidence", self._no_evidence)
        builder.add_node("safe_fail", self._safe_fail)
        builder.add_node("finish", lambda state: {})

        builder.add_edge(START, "normalize")
        builder.add_conditional_edges(
            "normalize",
            self._route_after_normalize,
            {
                "rewrite_question": "rewrite_question",
                "finish": "finish",
            },
        )
        builder.add_edge("rewrite_question", "prelink_entities")
        builder.add_edge("prelink_entities", "classify_intent")
        builder.add_conditional_edges(
            "classify_intent",
            self._route_after_classify,
            {
                "extract_entities": "extract_entities",
                "direct_answer": "direct_answer",
                "finish": "finish",
            },
        )
        builder.add_conditional_edges(
            "extract_entities",
            self._route_after_extract,
            {"link_entities": "link_entities", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "link_entities",
            self._route_after_link,
            {"plan_cypher": "plan_cypher", "clarify": "clarify"},
        )
        builder.add_edge("plan_cypher", "validate_cypher")
        builder.add_conditional_edges(
            "validate_cypher",
            self._route_after_validate,
            {
                "execute_cypher": "execute_cypher",
                "plan_cypher": "plan_cypher",
                "safe_fail": "safe_fail",
            },
        )
        builder.add_conditional_edges(
            "execute_cypher",
            self._route_after_execute,
            {
                "answer_from_graph": "answer_from_graph",
                "no_evidence": "no_evidence",
                "plan_cypher": "plan_cypher",
                "finish": "finish",
            },
        )
        builder.add_edge("answer_from_graph", "verify_answer")

        for terminal_node in (
            "verify_answer",
            "direct_answer",
            "clarify",
            "no_evidence",
            "safe_fail",
        ):
            builder.add_edge(terminal_node, END)
        builder.add_edge("finish", END)
        return builder.compile()

    def _terminal_error(
        self,
        state: AgentState,
        code: ErrorCode,
        message: str,
        answer: str,
        stage: str,
    ) -> Dict[str, Any]:
        _emit_answer_delta(answer)
        return {
            "answer": answer,
            "grounded": False,
            "error_code": code.value,
            "error_message": message,
            "stage_events": [stage],
        }

    def _normalize(self, state: AgentState) -> Dict[str, Any]:
        question = state.get("question", "")
        try:
            request = GraphQARequest(question=question)
        except ValidationError:
            return self._terminal_error(
                state,
                ErrorCode.INVALID_REQUEST,
                "问题必须是非空文本且不超过 500 个字符",
                "问题格式无效，请输入一个具体的中医知识问题。",
                "normalize",
            )

        normalized = normalize_question(request.question)
        if not normalized or len(normalized) > 500:
            return self._terminal_error(
                state,
                ErrorCode.INVALID_REQUEST,
                "规范化后的问题为空或超长",
                "问题格式无效，请输入一个具体的中医知识问题。",
                "normalize",
            )
        unsafe_reason = detect_unsafe_question(normalized)
        if unsafe_reason:
            return self._terminal_error(
                state,
                ErrorCode.UNSAFE_REQUEST,
                unsafe_reason,
                "该请求未执行。系统只支持中医知识图谱的只读查询。",
                "normalize",
            )
        return {
            "normalized_question": normalized,
            "rewritten_question": normalized,
            "query_attempts": 0,
            "stage_events": ["normalize"],
        }

    def _rewrite_question(self, state: AgentState) -> Dict[str, Any]:
        question = state["normalized_question"]
        history = state.get("conversation_history", [])
        explicit_entities = self.entity_index.match_exact(
            question,
            limit_per_label=self.entity_limit,
        )
        if (
            not history
            or explicit_entities
            or not _looks_context_dependent(question)
        ):
            return {
                "rewritten_question": question,
                "stage_events": ["rewrite_question"],
            }
        previous_turn = history[-1]
        if (
            not previous_turn.linked_entities
            and "请补充" in previous_turn.answer
            and any(pronoun in question for pronoun in CONTEXT_PRONOUNS)
        ):
            return {
                "rewritten_question": question,
                "warnings": [
                    "上一轮未确认具体实体，本轮需要继续澄清指代。"
                ],
                "stage_events": ["rewrite_question"],
            }

        try:
            result = self.agents.dialogue.rewrite_question(
                question,
                history,
            )
            rewritten = normalize_question(result.standalone_question)
            if not rewritten or len(rewritten) > 500:
                raise ValueError("改写后的问题无效")
            if detect_unsafe_question(rewritten):
                raise ValueError("改写后的问题违反只读规则")
            return {
                "rewritten_question": rewritten,
                "stage_events": ["rewrite_question"],
            }
        except Exception:
            contextual = _recent_contextual_question(question, history)
            if contextual:
                return {
                    "rewritten_question": contextual,
                    "warnings": [
                        "对话改写模型不可用，已使用最近有效实体补全问题。"
                    ],
                    "stage_events": ["rewrite_question"],
                }
            return {
                "rewritten_question": question,
                "warnings": ["对话改写不可用，已按当前问题继续处理。"],
                "stage_events": ["rewrite_question"],
            }

    def _classify_intent(self, state: AgentState) -> Dict[str, Any]:
        prelinked_entities = state.get("prelinked_entities", [])
        if prelinked_entities:
            return {
                "intent": IntentResult(
                    is_tcm_question=True,
                    confidence=1.0,
                    matched_domain_terms=[item.name for item in prelinked_entities],
                    reason_code="KNOWN_GRAPH_ENTITY",
                ),
                "stage_events": ["classify_intent"],
            }
        try:
            intent = self.agents.intent.classify(_active_question(state))
            return {"intent": intent, "stage_events": ["classify_intent"]}
        except ReasonerError as exc:
            return self._terminal_error(
                state,
                exc.code,
                exc.message,
                "模型服务暂时不可用，请稍后重试。",
                "classify_intent",
            )
        except Exception:
            return self._terminal_error(
                state,
                ErrorCode.INTERNAL_ERROR,
                "意图识别阶段发生内部错误",
                "系统处理问题失败，请稍后重试。",
                "classify_intent",
            )

    def _extract_entities(self, state: AgentState) -> Dict[str, Any]:
        if state.get("prelinked_entities"):
            return {
                "entity_candidates": [],
                "stage_events": ["extract_entities"],
            }
        try:
            candidates = self.agents.entity.extract(_active_question(state))
            return {
                "entity_candidates": candidates,
                "stage_events": ["extract_entities"],
            }
        except ReasonerError as exc:
            return self._terminal_error(
                state,
                exc.code,
                exc.message,
                "模型服务暂时不可用，请稍后重试。",
                "extract_entities",
            )
        except Exception:
            return self._terminal_error(
                state,
                ErrorCode.INTERNAL_ERROR,
                "实体抽取阶段发生内部错误",
                "系统处理问题失败，请稍后重试。",
                "extract_entities",
            )

    def _link_entities(self, state: AgentState) -> Dict[str, Any]:
        candidate_linked = []
        if state.get("entity_candidates"):
            candidate_linked = self.entity_index.match(
                state["entity_candidates"],
                _active_question(state),
                limit_per_label=self.entity_limit,
            )
        best_by_key = {
            (item.label, item.name): item
            for item in [*state.get("prelinked_entities", []), *candidate_linked]
        }
        linked = sorted(
            best_by_key.values(),
            key=lambda item: (-item.score, item.label, item.name),
        )
        return {
            "linked_entities": linked,
            "stage_events": ["link_entities"],
        }

    def _build_attribute_plan(
        self,
        state: AgentState,
    ) -> Optional[AttributeQueryPlan]:
        if state.get("attribute_query_attempted"):
            return None
        requested = set(_requested_attributes(_active_question(state)))
        if not requested:
            return None

        used_entities = []
        selections_by_label: Dict[str, Set[str]] = {}
        for entity in state.get("linked_entities", []):
            queryable = set(self.schema.queryable_attributes(entity.label))
            selected = requested & queryable
            if not selected:
                continue
            used_entities.append(
                {
                    "label": entity.label,
                    "name": entity.name,
                }
            )
            selections_by_label.setdefault(entity.label, set()).update(selected)
        if not used_entities:
            return None
        return AttributeQueryPlan(
            used_entities=used_entities,
            selections=[
                AttributeSelection(
                    label=label,
                    attributes=sorted(attributes),
                )
                for label, attributes in sorted(selections_by_label.items())
            ],
        )

    def _select_relationship_pattern(
        self,
        state: AgentState,
    ) -> Optional[RelationshipSelection]:
        question = _active_question(state)
        requested_relations = _requested_relations(question)
        if not requested_relations:
            return None
        hints = _entity_hints(question)
        all_linked_entities = state.get("linked_entities", [])
        linked_by_label: Dict[str, List[Any]] = {}
        for entity in all_linked_entities:
            linked_by_label.setdefault(entity.label, []).append(entity)

        candidates: List[Tuple[int, RelationshipSelection]] = []
        for subject_label, relation, object_label in self.schema.triple_patterns:
            if relation not in requested_relations:
                continue
            direction_hint = _relationship_direction_hint(
                question,
                all_linked_entities,
                relation,
            )
            subject_entities = linked_by_label.get(subject_label, [])
            object_entities = linked_by_label.get(object_label, [])

            if subject_entities:
                score = 1
                if object_label in hints:
                    score += 4
                if hints and subject_label in hints and object_label not in hints:
                    score -= 2
                if direction_hint == "forward":
                    score += 5
                elif direction_hint == "reverse":
                    score -= 5
                candidates.append(
                    (
                        score,
                        RelationshipSelection(
                            subject_label,
                            relation,
                            object_label,
                            "outgoing",
                            tuple(subject_entities),
                        ),
                    )
                )
            if object_entities:
                score = 1
                if subject_label in hints:
                    score += 4
                if hints and object_label in hints and subject_label not in hints:
                    score -= 2
                if any(marker in question for marker in ("哪些", "什么")):
                    score += 1
                if direction_hint == "reverse":
                    score += 5
                elif direction_hint == "forward":
                    score -= 5
                candidates.append(
                    (
                        score,
                        RelationshipSelection(
                            subject_label,
                            relation,
                            object_label,
                            "incoming",
                            tuple(object_entities),
                        ),
                    )
                )

        if not candidates:
            return None
        best_score = max(score for score, _selection in candidates)
        best = [
            selection
            for score, selection in candidates
            if score == best_score
        ]
        unique = {
            (
                selection.subject_label,
                selection.relationship,
                selection.object_label,
                selection.direction,
                tuple(item.name for item in selection.entities),
            ): selection
            for selection in best
        }
        if len(unique) != 1:
            return None
        return next(iter(unique.values()))

    def _build_relationship_template_plan(
        self,
        state: AgentState,
    ) -> Optional[CypherPlan]:
        selection = self._select_relationship_pattern(state)
        if selection is None:
            return None
        return self._relationship_template(
            selection.subject_label,
            selection.relationship,
            selection.object_label,
            selection.entities,
            reverse=selection.direction == "incoming",
        )

    def _build_aggregate_plan(
        self,
        state: AgentState,
    ) -> Optional[AggregateQueryPlan]:
        if not _is_count_question(_active_question(state)):
            return None
        selection = self._select_relationship_pattern(state)
        if selection is None:
            return None
        result_label = (
            selection.object_label
            if selection.direction == "outgoing"
            else selection.subject_label
        )
        anchor_names = sorted(
            {item.name for item in selection.entities}
        )
        require_all_entities = (
            len(anchor_names) > 1
            and selection.direction == "incoming"
            and _has_explicit_conjunction(_active_question(state))
        )
        if (
            len(anchor_names) > 1
            and selection.direction == "outgoing"
            and _has_explicit_conjunction(_active_question(state))
        ):
            return None
        return AggregateQueryPlan(
            subject_label=selection.subject_label,
            relationship=selection.relationship,
            object_label=selection.object_label,
            used_entities=[
                {
                    "label": item.label,
                    "name": item.name,
                }
                for item in sorted(
                    selection.entities,
                    key=lambda entity: (entity.label, entity.name),
                )
            ],
            direction=selection.direction,
            require_all_entities=require_all_entities,
            description=_aggregate_description(
                anchor_names,
                result_label,
                selection.direction,
            ),
        )

    def _build_multi_condition_list_plan(
        self,
        state: AgentState,
    ) -> Optional[MultiConditionQueryPlan]:
        if _is_count_question(_active_question(state)):
            return None
        selection = self._select_relationship_pattern(state)
        if selection is None or selection.direction != "incoming":
            return None
        anchor_names = sorted(
            {item.name for item in selection.entities}
        )
        if (
            len(anchor_names) < 2
            or not _has_explicit_conjunction(_active_question(state))
        ):
            return None
        return MultiConditionQueryPlan(
            subject_label=selection.subject_label,
            relationship=selection.relationship,
            object_label=selection.object_label,
            used_entities=[
                {
                    "label": item.label,
                    "name": item.name,
                }
                for item in sorted(
                    selection.entities,
                    key=lambda entity: (entity.label, entity.name),
                )
            ],
            description=_aggregate_description(
                anchor_names,
                selection.subject_label,
                selection.direction,
            ),
        )

    def _build_multi_hop_plan(
        self,
        state: AgentState,
    ) -> Optional[MultiHopQueryPlan]:
        question = _active_question(state)
        requested_relations = _requested_relations(question)
        if len(requested_relations) < 2:
            return None
        linked_by_label: Dict[str, List[Any]] = {}
        for entity in state.get("linked_entities", []):
            linked_by_label.setdefault(entity.label, []).append(entity)

        candidates = []
        for first_step, second_step, result_description in MULTI_HOP_CATALOG:
            if {
                first_step[1],
                second_step[1],
            } - requested_relations:
                continue
            entities = linked_by_label.get(first_step[0], [])
            if not entities:
                continue
            try:
                self.schema.assert_pattern(*first_step)
                self.schema.assert_pattern(*second_step)
            except Exception:
                continue
            candidates.append((first_step, second_step, result_description, entities))
        if not candidates:
            return None
        unique = {
            (
                first_step,
                second_step,
                tuple(item.label for item in entities),
                tuple(item.name for item in entities),
            ): (first_step, second_step, result_description, entities)
            for first_step, second_step, result_description, entities in candidates
        }
        if len(unique) != 1:
            return None
        first_step, second_step, result_description, entities = next(
            iter(unique.values())
        )
        anchor_names = sorted({item.name for item in entities})
        return MultiHopQueryPlan(
            steps=[
                MultiHopStep(
                    subject_label=first_step[0],
                    relationship=first_step[1],
                    object_label=first_step[2],
                ),
                MultiHopStep(
                    subject_label=second_step[0],
                    relationship=second_step[1],
                    object_label=second_step[2],
                ),
            ],
            used_entities=[
                {
                    "label": item.label,
                    "name": item.name,
                }
                for item in sorted(
                    entities,
                    key=lambda entity: (entity.label, entity.name),
                )
            ],
            description=f"{'、'.join(anchor_names)}{result_description}",
        )

    def _relationship_template(
        self,
        subject_label: str,
        relation: str,
        object_label: str,
        target_entities: Sequence[Any],
        *,
        reverse: bool,
    ) -> CypherPlan:
        parameter_target = "object" if reverse else "subject"
        limit = min(20, self.guard.max_results)
        can_load_source = relation != "FROM_SOURCE" and (
            subject_label,
            "FROM_SOURCE",
            "Source",
        ) in self.schema.triple_patterns
        source_match = (
            "\nOPTIONAL MATCH (subject)-[:FROM_SOURCE]->(source_node:Source)"
            if can_load_source
            else ""
        )
        source_return = (
            "source_node.name AS source"
            if can_load_source
            else "NULL AS source"
        )
        cypher = f"""
MATCH (subject:{subject_label})-[relationship:{relation}]->(object:{object_label})
WHERE {parameter_target}.name IN $entity_names
{source_match}
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       type(relationship) AS relationship,
       object.name AS object,
       labels(object)[0] AS object_label,
       {source_return}
LIMIT {limit}
""".strip()
        names = sorted({entity.name for entity in target_entities})
        return CypherPlan(
            cypher=cypher,
            parameters={"entity_names": names},
            used_entities=[
                {
                    "label": entity.label,
                    "name": entity.name,
                }
                for entity in sorted(
                    target_entities,
                    key=lambda item: (item.label, item.name),
                )
            ],
            expected_result_shape=(
                f"{subject_label.lower()}_{relation.lower()}_"
                f"{object_label.lower()}"
            ),
        )

    def _plan_cypher(self, state: AgentState) -> Dict[str, Any]:
        linked_entities = state.get("linked_entities", [])
        multi_hop_plan = self._build_multi_hop_plan(state)
        if multi_hop_plan is not None:
            return {
                "multi_hop_plan": multi_hop_plan,
                "aggregate_plan": None,
                "attribute_plan": None,
                "attribute_relation_fallback": False,
                "cypher_plan": None,
                "query_attempts": state.get("query_attempts", 0),
                "stage_events": ["plan_cypher"],
            }
        attribute_plan = self._build_attribute_plan(state)
        if attribute_plan is not None:
            return {
                "attribute_plan": attribute_plan,
                "attribute_query_attempted": True,
                "attribute_relation_fallback": False,
                "cypher_plan": None,
                "query_attempts": state.get("query_attempts", 0),
                "stage_events": ["plan_cypher"],
            }
        aggregate_plan = self._build_aggregate_plan(state)
        if aggregate_plan is not None:
            return {
                "aggregate_plan": aggregate_plan,
                "attribute_plan": None,
                "attribute_relation_fallback": False,
                "cypher_plan": None,
                "query_attempts": state.get("query_attempts", 0),
                "stage_events": ["plan_cypher"],
            }
        multi_condition_plan = self._build_multi_condition_list_plan(state)
        if multi_condition_plan is not None:
            return {
                "aggregate_plan": None,
                "multi_condition_plan": multi_condition_plan,
                "attribute_plan": None,
                "attribute_relation_fallback": False,
                "cypher_plan": None,
                "query_attempts": state.get("query_attempts", 0),
                "stage_events": ["plan_cypher"],
            }
        template_plan = self._build_relationship_template_plan(state)
        if template_plan is not None:
            return {
                "attribute_plan": None,
                "attribute_relation_fallback": False,
                "cypher_plan": template_plan,
                "query_attempts": state.get("query_attempts", 0),
                "stage_events": ["plan_cypher"],
            }
        attempts = state.get("query_attempts", 0) + 1
        try:
            plan = self.reasoner.generate_cypher(
                _active_question(state),
                linked_entities,
                self.schema,
                state.get("validation_errors", []),
            )
            linked_keys = {(item.label, item.name) for item in linked_entities}
            used_keys = {(item.label, item.name) for item in plan.used_entities}
            if not used_keys or not used_keys.issubset(linked_keys):
                return {
                    "cypher_plan": None,
                    "query_attempts": attempts,
                    "validation_errors": ["模型引用了未链接的实体"],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "stage_events": ["plan_cypher"],
                }
            parameter_names = {
                value
                for parameter_value in plan.parameters.values()
                for value in _parameter_strings(parameter_value)
            }
            if not any(item.name in parameter_names for item in linked_entities):
                return {
                    "cypher_plan": None,
                    "query_attempts": attempts,
                    "validation_errors": ["查询参数没有包含已链接实体名称"],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "stage_events": ["plan_cypher"],
                }
            return {
                "attribute_plan": None,
                "attribute_relation_fallback": False,
                "cypher_plan": plan,
                "query_attempts": attempts,
                "stage_events": ["plan_cypher"],
            }
        except ReasonerError as exc:
            return {
                "cypher_plan": None,
                "query_attempts": attempts,
                "validation_errors": [exc.message],
                "last_error_code": exc.code.value,
                "stage_events": ["plan_cypher"],
            }
        except Exception:
            return {
                "cypher_plan": None,
                "query_attempts": attempts,
                "validation_errors": ["生成查询时发生内部错误"],
                "last_error_code": ErrorCode.INTERNAL_ERROR.value,
                "stage_events": ["plan_cypher"],
            }

    def _validate_cypher(self, state: AgentState) -> Dict[str, Any]:
        multi_hop_plan = state.get("multi_hop_plan")
        if multi_hop_plan is not None:
            try:
                if len(multi_hop_plan.steps) != 2:
                    raise ValueError("多跳步骤数量错误")
                first, second = multi_hop_plan.steps
                if first.object_label != second.subject_label:
                    raise ValueError("多跳节点链不连续")
                self.schema.assert_pattern(
                    first.subject_label,
                    first.relationship,
                    first.object_label,
                )
                self.schema.assert_pattern(
                    second.subject_label,
                    second.relationship,
                    second.object_label,
                )
                if any(
                    entity.label != first.subject_label
                    for entity in multi_hop_plan.used_entities
                ):
                    raise ValueError("多跳起始实体不匹配")
            except Exception:
                return {
                    "multi_hop_plan": None,
                    "is_cypher_valid": False,
                    "validation_errors": [
                        f"{ErrorCode.CYPHER_REJECTED.value}: "
                        "多跳查询包含未允许的模式"
                    ],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "query_attempts": self.max_attempts,
                    "stage_events": ["validate_cypher"],
                }
            return {
                "is_cypher_valid": True,
                "stage_events": ["validate_cypher"],
            }

        multi_condition_plan = state.get("multi_condition_plan")
        if multi_condition_plan is not None:
            try:
                self.schema.assert_pattern(
                    multi_condition_plan.subject_label,
                    multi_condition_plan.relationship,
                    multi_condition_plan.object_label,
                )
                if any(
                    entity.label != multi_condition_plan.object_label
                    for entity in multi_condition_plan.used_entities
                ):
                    raise ValueError("多条件实体标签不匹配")
                if len(multi_condition_plan.used_entities) < 2:
                    raise ValueError("多条件实体数量不足")
            except Exception:
                return {
                    "multi_condition_plan": None,
                    "is_cypher_valid": False,
                    "validation_errors": [
                        f"{ErrorCode.CYPHER_REJECTED.value}: "
                        "多条件查询包含未允许的模式"
                    ],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "query_attempts": self.max_attempts,
                    "stage_events": ["validate_cypher"],
                }
            return {
                "is_cypher_valid": True,
                "stage_events": ["validate_cypher"],
            }

        aggregate_plan = state.get("aggregate_plan")
        if aggregate_plan is not None:
            try:
                self.schema.assert_pattern(
                    aggregate_plan.subject_label,
                    aggregate_plan.relationship,
                    aggregate_plan.object_label,
                )
                anchor_label = (
                    aggregate_plan.subject_label
                    if aggregate_plan.direction == "outgoing"
                    else aggregate_plan.object_label
                )
                if any(
                    entity.label != anchor_label
                    for entity in aggregate_plan.used_entities
                ):
                    raise ValueError("聚合方向与实体不匹配")
            except Exception:
                return {
                    "aggregate_plan": None,
                    "is_cypher_valid": False,
                    "validation_errors": [
                        f"{ErrorCode.CYPHER_REJECTED.value}: "
                        "聚合查询包含未允许的模式"
                    ],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "query_attempts": self.max_attempts,
                    "stage_events": ["validate_cypher"],
                }
            return {
                "is_cypher_valid": True,
                "stage_events": ["validate_cypher"],
            }

        attribute_plan = state.get("attribute_plan")
        if attribute_plan is not None:
            try:
                selected_by_label = {
                    selection.label: set(selection.attributes)
                    for selection in attribute_plan.selections
                }
                for entity in attribute_plan.used_entities:
                    allowed = set(
                        self.schema.queryable_attributes(entity.label)
                    )
                    requested = selected_by_label.get(entity.label, set())
                    if not requested or not requested.issubset(allowed):
                        raise ValueError("属性查询不在白名单")
            except Exception:
                return {
                    "attribute_plan": None,
                    "is_cypher_valid": False,
                    "validation_errors": [
                        f"{ErrorCode.CYPHER_REJECTED.value}: "
                        "属性查询包含未允许的字段"
                    ],
                    "last_error_code": ErrorCode.CYPHER_REJECTED.value,
                    "query_attempts": self.max_attempts,
                    "stage_events": ["validate_cypher"],
                }
            return {
                "is_cypher_valid": True,
                "stage_events": ["validate_cypher"],
            }

        plan = state.get("cypher_plan")
        if plan is None:
            return {
                "is_cypher_valid": False,
                "stage_events": ["validate_cypher"],
            }
        try:
            self.guard.validate(plan)
            self.repository.explain(plan)
        except CypherGuardError as exc:
            return {
                "cypher_plan": None,
                "is_cypher_valid": False,
                "validation_errors": [f"{exc.code.value}: {exc.message}"],
                "last_error_code": exc.code.value,
                "stage_events": ["validate_cypher"],
            }
        except RepositoryError as exc:
            return {
                "cypher_plan": None,
                "is_cypher_valid": False,
                "validation_errors": [f"{exc.code.value}: {exc.message}"],
                "last_error_code": exc.code.value,
                "stage_events": ["validate_cypher"],
            }
        except Exception:
            return {
                "cypher_plan": None,
                "is_cypher_valid": False,
                "validation_errors": [f"{ErrorCode.CYPHER_INVALID.value}: 查询校验失败"],
                "last_error_code": ErrorCode.CYPHER_INVALID.value,
                "stage_events": ["validate_cypher"],
            }
        return {
            "is_cypher_valid": True,
            "stage_events": ["validate_cypher"],
        }

    def _execute_cypher(self, state: AgentState) -> Dict[str, Any]:
        multi_hop_plan = state.get("multi_hop_plan")
        if multi_hop_plan is not None:
            try:
                result = self.repository.execute_multi_hop(multi_hop_plan)
            except RepositoryError as exc:
                return self._terminal_error(
                    state,
                    exc.code,
                    exc.message,
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            except Exception:
                return self._terminal_error(
                    state,
                    ErrorCode.QUERY_EXECUTION_FAILED,
                    "多跳查询发生内部错误",
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            first, second = multi_hop_plan.steps
            paths: List[PathEvidence] = []
            seen_paths = set()
            for row in result.rows:
                root = row.get("root")
                root_label = row.get("root_label")
                intermediate = row.get("intermediate")
                intermediate_label = row.get("intermediate_label")
                target = row.get("target")
                target_label = row.get("target_label")
                first_relationship = row.get("first_relationship")
                second_relationship = row.get("second_relationship")
                if (
                    not isinstance(root, str)
                    or not isinstance(intermediate, str)
                    or not isinstance(target, str)
                    or root_label != first.subject_label
                    or intermediate_label != first.object_label
                    or target_label != second.object_label
                    or first_relationship != first.relationship
                    or second_relationship != second.relationship
                ):
                    continue
                sources = row.get("sources")
                source = None
                if isinstance(sources, list):
                    source = "；".join(
                        item
                        for item in sources
                        if isinstance(item, str) and item
                    ) or None
                key = (
                    root,
                    intermediate,
                    target,
                    first_relationship,
                    second_relationship,
                    source,
                )
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                paths.append(
                    PathEvidence(
                        root=root,
                        root_label=root_label,
                        first_relationship=first_relationship,
                        intermediate=intermediate,
                        intermediate_label=intermediate_label,
                        second_relationship=second_relationship,
                        target=target,
                        target_label=target_label,
                        source=source,
                    )
                )
            update: Dict[str, Any] = {
                "query_result": result,
                "evidence": [],
                "facts": [],
                "aggregates": [],
                "paths": paths,
                "stage_events": ["execute_cypher"],
            }
            if result.rows and not paths:
                update.update(
                    {
                        "error_code": ErrorCode.NO_GRAPH_EVIDENCE.value,
                        "error_message": "多跳查询没有返回可验证路径",
                        "answer": NO_EVIDENCE_ANSWER,
                        "grounded": False,
                    }
                )
            return update

        multi_condition_plan = state.get("multi_condition_plan")
        if multi_condition_plan is not None:
            try:
                result = self.repository.execute_multi_condition_list(
                    multi_condition_plan
                )
            except RepositoryError as exc:
                return self._terminal_error(
                    state,
                    exc.code,
                    exc.message,
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            except Exception:
                return self._terminal_error(
                    state,
                    ErrorCode.QUERY_EXECUTION_FAILED,
                    "多条件查询发生内部错误",
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            expected_names = {
                entity.name
                for entity in multi_condition_plan.used_entities
            }
            evidence_rows = []
            for row in result.rows:
                subject = row.get("subject")
                subject_label = row.get("subject_label")
                conditions = row.get("conditions")
                if (
                    not isinstance(subject, str)
                    or not isinstance(subject_label, str)
                    or not isinstance(conditions, list)
                ):
                    continue
                for condition in conditions:
                    if not isinstance(condition, Mapping):
                        continue
                    object_name = condition.get("object")
                    object_label = condition.get("object_label")
                    relationship = condition.get("relationship")
                    if (
                        object_name not in expected_names
                        or object_label != multi_condition_plan.object_label
                        or relationship != multi_condition_plan.relationship
                    ):
                        continue
                    sources = condition.get("sources")
                    source = None
                    if isinstance(sources, list):
                        source = "；".join(
                            item
                            for item in sources
                            if isinstance(item, str) and item
                        ) or None
                    evidence_rows.append(
                        {
                            "subject": subject,
                            "subject_label": subject_label,
                            "relationship": relationship,
                            "object": object_name,
                            "object_label": object_label,
                            "source": source,
                        }
                    )
            evidence = evidence_from_rows(evidence_rows, self.schema)
            update: Dict[str, Any] = {
                "query_result": result,
                "evidence": evidence,
                "facts": [],
                "aggregates": [],
                "stage_events": ["execute_cypher"],
            }
            if result.rows and not evidence:
                update.update(
                    {
                        "error_code": ErrorCode.NO_GRAPH_EVIDENCE.value,
                        "error_message": "多条件查询没有返回可验证证据",
                        "answer": NO_EVIDENCE_ANSWER,
                        "grounded": False,
                    }
                )
            return update

        aggregate_plan = state.get("aggregate_plan")
        if aggregate_plan is not None:
            try:
                result = self.repository.execute_aggregate(aggregate_plan)
            except RepositoryError as exc:
                return self._terminal_error(
                    state,
                    exc.code,
                    exc.message,
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            except Exception:
                return self._terminal_error(
                    state,
                    ErrorCode.QUERY_EXECUTION_FAILED,
                    "聚合查询发生内部错误",
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            count = (
                result.rows[0].get("count")
                if result.rows
                else None
            )
            if not isinstance(count, int) or isinstance(count, bool):
                return self._terminal_error(
                    state,
                    ErrorCode.QUERY_EXECUTION_FAILED,
                    "聚合查询没有返回有效计数",
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            result_label = (
                aggregate_plan.object_label
                if aggregate_plan.direction == "outgoing"
                else aggregate_plan.subject_label
            )
            aggregate = AggregateFact(
                anchor="、".join(
                    sorted(
                        {
                            item.name
                            for item in aggregate_plan.used_entities
                        }
                    )
                ),
                anchor_label=aggregate_plan.used_entities[0].label,
                relationship=aggregate_plan.relationship,
                result_label=result_label,
                direction=aggregate_plan.direction,
                count=count,
                description=aggregate_plan.description,
            )
            return {
                "query_result": result,
                "evidence": [],
                "facts": [],
                "aggregates": [aggregate],
                "stage_events": ["execute_cypher"],
            }

        attribute_plan = state.get("attribute_plan")
        if attribute_plan is not None:
            try:
                result = self.repository.execute_attributes(attribute_plan)
            except RepositoryError as exc:
                return self._terminal_error(
                    state,
                    exc.code,
                    exc.message,
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )
            except Exception:
                return self._terminal_error(
                    state,
                    ErrorCode.QUERY_EXECUTION_FAILED,
                    "属性查询发生内部错误",
                    "图谱查询失败，请稍后重试。",
                    "execute_cypher",
                )

            facts = facts_from_rows(result.rows, attribute_plan)
            update: Dict[str, Any] = {
                "query_result": result,
                "evidence": [],
                "facts": facts,
                "stage_events": ["execute_cypher"],
            }
            requested_attributes = {
                attribute
                for selection in attribute_plan.selections
                for attribute in selection.attributes
            }
            if (
                not facts
                and requested_attributes
                and requested_attributes.issubset(
                    RELATION_FALLBACK_ATTRIBUTES
                )
            ):
                update.update(
                    {
                        "attribute_plan": None,
                        "attribute_relation_fallback": True,
                        "query_result": None,
                        "warnings": [
                            "属性字段没有可用值，已尝试通过图谱关系回答。"
                        ],
                    }
                )
            return update

        plan = state.get("cypher_plan")
        if plan is None:
            return self._terminal_error(
                state,
                ErrorCode.INTERNAL_ERROR,
                "执行阶段缺少查询计划",
                "系统处理问题失败，请稍后重试。",
                "execute_cypher",
            )
        try:
            result = self.repository.execute_read(plan)
        except RepositoryError as exc:
            return self._terminal_error(
                state,
                exc.code,
                exc.message,
                "图谱查询失败，请稍后重试。",
                "execute_cypher",
            )
        except Exception:
            return self._terminal_error(
                state,
                ErrorCode.QUERY_EXECUTION_FAILED,
                "图谱查询发生内部错误",
                "图谱查询失败，请稍后重试。",
                "execute_cypher",
            )

        evidence = evidence_from_rows(result.rows, self.schema)
        target_evidence = _relevant_evidence(evidence, plan.used_entities)
        update: Dict[str, Any] = {
            "query_result": result,
            "evidence": target_evidence,
            "facts": [],
            "stage_events": ["execute_cypher"],
        }
        if result.rows and not target_evidence:
            update.update(
                {
                    "error_code": ErrorCode.NO_GRAPH_EVIDENCE.value,
                    "error_message": "查询结果没有关联目标实体的可验证证据",
                    "answer": NO_EVIDENCE_ANSWER,
                    "grounded": False,
                    "evidence": [],
                }
            )
        return update

    def _answer_from_graph(self, state: AgentState) -> Dict[str, Any]:
        evidence = state.get("evidence", [])
        facts = state.get("facts", [])
        aggregates = state.get("aggregates", [])
        paths = state.get("paths", [])
        query_result = state.get("query_result")
        plan = state.get("cypher_plan")
        attribute_plan = state.get("attribute_plan")
        aggregate_plan = state.get("aggregate_plan")
        multi_condition_plan = state.get("multi_condition_plan")
        multi_hop_plan = state.get("multi_hop_plan")
        relation_context_valid = bool(
            evidence
            and plan is not None
            and evidence_matches_used_entities(
                evidence,
                plan.used_entities,
            )
        )
        attribute_context_valid = bool(
            facts
            and attribute_plan is not None
            and facts_match_used_entities(
                facts,
                attribute_plan.used_entities,
            )
        )
        aggregate_context_valid = bool(
            aggregates
            and aggregate_plan is not None
        )
        multi_condition_context_valid = bool(
            evidence
            and multi_condition_plan is not None
            and evidence_matches_used_entities(
                evidence,
                multi_condition_plan.used_entities,
            )
        )
        multi_hop_context_valid = bool(
            paths
            and multi_hop_plan is not None
        )
        if query_result is None or not (
            relation_context_valid
            or attribute_context_valid
            or aggregate_context_valid
            or multi_condition_context_valid
            or multi_hop_context_valid
        ):
            return self._terminal_error(
                state,
                ErrorCode.NO_GRAPH_EVIDENCE,
                "没有关联目标实体的可验证图谱证据",
                NO_EVIDENCE_ANSWER,
                "answer_from_graph",
            )
        if multi_hop_plan is not None and paths:
            answer = append_disclaimer(
                _multi_hop_answer(multi_hop_plan, paths)
            )
            _emit_answer_delta(answer)
            return {
                "answer": answer,
                "grounded": True,
                "stage_events": ["answer_from_graph"],
            }
        if multi_condition_plan is not None and evidence:
            result_names = list(
                dict.fromkeys(item.subject for item in evidence)
            )
            lines = [
                f"根据当前图谱，{multi_condition_plan.description}如下：",
                *[f"- {name}" for name in result_names],
            ]
            answer = append_disclaimer("\n".join(lines))
            _emit_answer_delta(answer)
            return {
                "answer": answer,
                "grounded": True,
                "stage_events": ["answer_from_graph"],
            }
        if aggregates and not evidence:
            answer = append_disclaimer(_aggregate_answer(aggregates))
            _emit_answer_delta(answer)
            return {
                "answer": answer,
                "grounded": True,
                "stage_events": ["answer_from_graph"],
            }
        if facts and not evidence:
            answer = append_disclaimer(
                _context_fallback_answer([], facts)
            )
            _emit_answer_delta(answer)
            return {
                "answer": answer,
                "grounded": True,
                "stage_events": ["answer_from_graph"],
            }
        parts: List[str] = []

        def on_token(token: str) -> None:
            if token:
                parts.append(token)
                _emit_answer_delta(token)

        try:
            streamed = self.agents.answer.stream_graph_answer(
                _active_question(state),
                state.get("linked_entities", []),
                query_result.rows,
                evidence,
                facts,
                on_token,
            )
            if streamed is not None:
                answer = "".join(parts) or streamed
            else:
                answer = self.agents.answer.graph_answer(
                    _active_question(state),
                    state.get("linked_entities", []),
                    query_result.rows,
                    evidence,
                    facts,
                )
                _emit_answer_delta(answer)
            final_answer = append_disclaimer(
                _normalize_answer_formatting(answer, facts)
            )
            emitted = "".join(parts)
            if final_answer.startswith(emitted):
                _emit_answer_delta(final_answer[len(emitted) :])
            return {
                "answer": final_answer,
                "grounded": True,
                "stage_events": ["answer_from_graph"],
            }
        except Exception:
            if parts:
                final_answer = append_disclaimer(
                    _normalize_answer_formatting("".join(parts), facts)
                )
                _emit_answer_delta(final_answer[len("".join(parts)) :])
                return {
                    "answer": final_answer,
                    "grounded": True,
                    "warnings": ["回答流式生成中断，内容可能不完整。"],
                    "stage_events": ["answer_from_graph"],
                }
            fallback = append_disclaimer(_evidence_fallback_answer(evidence))
            if facts:
                fallback = append_disclaimer(
                    _context_fallback_answer(evidence, facts)
                )
            _emit_answer_delta(fallback)
            return {
                "answer": fallback,
                "grounded": True,
                "warnings": ["回答生成模型不可用，已使用图谱证据生成简化回答。"],
                "stage_events": ["answer_from_graph"],
            }

    def _verify_answer(self, state: AgentState) -> Dict[str, Any]:
        answer = state.get("answer", "").strip()
        evidence = state.get("evidence", [])
        facts = state.get("facts", [])
        if not answer or not _answer_supported_by_context(
            answer,
            evidence,
            facts,
            state.get("aggregates", []),
            state.get("paths", []),
        ):
            fallback = append_disclaimer(
                _aggregate_answer(state.get("aggregates", []))
                if state.get("aggregates")
                else _context_fallback_answer(evidence, facts)
            )
            return {
                "answer": fallback,
                "grounded": True,
                "warnings": [
                    "回答模型输出的内容未能完整对应本轮证据，"
                    "已改为证据化回答。"
                ],
                "stage_events": ["verify_answer"],
            }
        return {
            "answer": answer,
            "grounded": True,
            "stage_events": ["verify_answer"],
        }

    def _direct_answer(self, state: AgentState) -> Dict[str, Any]:
        parts: List[str] = []

        def on_token(token: str) -> None:
            if token:
                parts.append(token)
                _emit_answer_delta(token)

        try:
            streamed = self.agents.answer.stream_direct_answer(
                _active_question(state),
                on_token,
            )
            if streamed is not None:
                answer = "".join(parts) or streamed
            else:
                answer = self.agents.answer.direct_answer(
                    _active_question(state)
                )
                _emit_answer_delta(answer)
            return {
                "answer": answer.strip(),
                "grounded": False,
                "warnings": ["该回答未使用知识图谱证据。"],
                "stage_events": ["direct_answer"],
            }
        except ReasonerError as exc:
            if parts:
                return {
                    "answer": "".join(parts).strip(),
                    "grounded": False,
                    "warnings": ["直接回答的流式输出中断。"],
                    "stage_events": ["direct_answer"],
                }
            return self._terminal_error(
                state,
                exc.code,
                exc.message,
                "模型服务暂时不可用，请稍后重试。",
                "direct_answer",
            )
        except Exception:
            if parts:
                return {
                    "answer": "".join(parts).strip(),
                    "grounded": False,
                    "warnings": ["直接回答的流式输出中断。"],
                    "stage_events": ["direct_answer"],
                }
            return self._terminal_error(
                state,
                ErrorCode.INTERNAL_ERROR,
                "直接回答阶段发生内部错误",
                "系统处理问题失败，请稍后重试。",
                "direct_answer",
            )

    def _clarify(self, state: AgentState) -> Dict[str, Any]:
        answer = append_disclaimer(CLARIFY_ANSWER)
        _emit_answer_delta(answer)
        return {
            "answer": answer,
            "grounded": False,
            "warnings": ["未找到可以用于图谱查询的具体实体。"],
            "stage_events": ["clarify"],
        }

    def _no_evidence(self, state: AgentState) -> Dict[str, Any]:
        answer = append_disclaimer(NO_EVIDENCE_ANSWER)
        _emit_answer_delta(answer)
        return {
            "answer": answer,
            "grounded": False,
            "warnings": ["图谱查询没有返回记录。"],
            "stage_events": ["no_evidence"],
        }

    def _safe_fail(self, state: AgentState) -> Dict[str, Any]:
        code_value = state.get("last_error_code") or ErrorCode.CYPHER_REJECTED.value
        try:
            code = ErrorCode(code_value)
        except ValueError:
            code = ErrorCode.CYPHER_REJECTED
        return self._terminal_error(
            state,
            code,
            "未生成符合安全规则的 Cypher",
            append_disclaimer(SAFE_RETRY_ANSWER),
            "safe_fail",
        )

    @staticmethod
    def _route_after_normalize(state: AgentState) -> str:
        return "finish" if state.get("error_code") else "rewrite_question"

    def _prelink_entities(self, state: AgentState) -> Dict[str, Any]:
        linked = self.entity_index.match_exact(
            _active_question(state),
            limit_per_label=self.entity_limit,
        )
        return {
            "prelinked_entities": linked,
            "stage_events": ["prelink_entities"],
        }

    @staticmethod
    def _route_after_classify(state: AgentState) -> str:
        if state.get("error_code"):
            return "finish"
        intent = state.get("intent")
        return "extract_entities" if intent and intent.is_tcm_question else "direct_answer"

    @staticmethod
    def _route_after_extract(state: AgentState) -> str:
        return "finish" if state.get("error_code") else "link_entities"

    @staticmethod
    def _route_after_link(state: AgentState) -> str:
        return "plan_cypher" if state.get("linked_entities") else "clarify"

    def _route_after_validate(self, state: AgentState) -> str:
        if state.get("is_cypher_valid"):
            return "execute_cypher"
        if state.get("query_attempts", 0) < self.max_attempts:
            return "plan_cypher"
        return "safe_fail"

    @staticmethod
    def _route_after_execute(state: AgentState) -> str:
        if state.get("error_code"):
            return "finish"
        if state.get("attribute_relation_fallback"):
            return "plan_cypher"
        if (
            state.get("aggregates")
            or state.get("facts")
            or state.get("evidence")
            or state.get("paths")
        ):
            return "answer_from_graph"
        result = state.get("query_result")
        if result is None or result.row_count == 0:
            return "no_evidence"
        return "no_evidence"

    def _initial_state(
        self,
        question: str,
        request_id: str,
        conversation_history: Optional[Sequence[ConversationTurn]],
        session_id: Optional[str],
    ) -> AgentState:
        return {
            "request_id": request_id,
            "question": question,
            "session_id": session_id,
            "conversation_history": list(conversation_history or []),
            "rewritten_question": None,
            "query_attempts": 0,
            "validation_errors": [],
            "warnings": [],
            "stage_events": [],
            "linked_entities": [],
            "prelinked_entities": [],
            "entity_candidates": [],
            "evidence": [],
            "intent": None,
            "attribute_plan": None,
            "attribute_query_attempted": False,
            "attribute_relation_fallback": False,
            "aggregate_plan": None,
            "multi_condition_plan": None,
            "multi_hop_plan": None,
            "cypher_plan": None,
            "query_result": None,
            "facts": [],
            "aggregates": [],
            "paths": [],
            "error_code": None,
            "error_message": None,
            "last_error_code": None,
            "is_cypher_valid": False,
            "fatal_error": None,
        }

    @staticmethod
    def _internal_error_response(
        request_id: str,
        question: str,
        session_id: Optional[str],
    ) -> GraphQAResponse:
        return GraphQAResponse(
            request_id=request_id,
            question=str(question),
            answer="系统处理问题失败，请稍后重试。",
            grounded=False,
            error={
                "code": ErrorCode.INTERNAL_ERROR,
                "message": "问答工作流发生内部错误",
            },
            session_id=session_id,
        )

    def _response_from_state(
        self,
        request_id: str,
        question: str,
        session_id: Optional[str],
        final_state: AgentState,
    ) -> GraphQAResponse:
        query_result = final_state.get("query_result")
        error_code = final_state.get("error_code")
        err = None
        if error_code:
            try:
                parsed_code = ErrorCode(error_code)
            except ValueError:
                parsed_code = ErrorCode.INTERNAL_ERROR
            err = {
                "code": parsed_code,
                "message": final_state.get("error_message") or "问答处理失败",
            }
        return GraphQAResponse(
            request_id=request_id,
            question=final_state.get("question", str(question)),
            answer=final_state.get("answer", "系统没有生成回答。"),
            grounded=bool(final_state.get("grounded", False)),
            intent=final_state.get("intent"),
            linked_entities=final_state.get("linked_entities", []),
            query=(
                QueryInfo(
                    cypher=query_result.cypher,
                    parameters=query_result.parameters,
                )
                if query_result is not None
                else None
            ),
            evidence=final_state.get("evidence", []),
            facts=final_state.get("facts", []),
            aggregates=final_state.get("aggregates", []),
            paths=final_state.get("paths", []),
            warnings=final_state.get("warnings", []),
            error=err,
            session_id=session_id,
            rewritten_question=final_state.get(
                "rewritten_question",
                final_state.get("normalized_question"),
            ),
        )

    def answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[Sequence[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> GraphQAResponse:
        request_id = request_id or str(uuid.uuid4())
        initial_state = self._initial_state(
            question,
            request_id,
            conversation_history,
            session_id,
        )
        try:
            final_state = self.graph.invoke(initial_state)
        except Exception:
            return self._internal_error_response(
                request_id,
                question,
                session_id,
            )
        return self._response_from_state(
            request_id,
            question,
            session_id,
            final_state,
        )

    def stream_answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[Sequence[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> Iterator[GraphQAStreamEvent]:
        request_id = request_id or str(uuid.uuid4())
        initial_state = self._initial_state(
            question,
            request_id,
            conversation_history,
            session_id,
        )
        final_state = initial_state
        try:
            for mode, payload in self.graph.stream(
                initial_state,
                stream_mode=["updates", "custom", "values"],
            ):
                if mode == "updates":
                    for node_name, update in payload.items():
                        agent = self.agents.agent_for_stage(node_name)
                        yield GraphQAStreamEvent(
                            event="stage",
                            data={
                                "stage": node_name,
                                "label": STAGE_LABELS.get(
                                    node_name,
                                    node_name,
                                ),
                                "agent": agent.name,
                            },
                        )
                        evidence = update.get("evidence", [])
                        if evidence:
                            yield GraphQAStreamEvent(
                                event="evidence",
                                data={
                                    "items": [
                                        item.model_dump(mode="json")
                                        for item in evidence
                                    ]
                                },
                            )
                        facts = update.get("facts", [])
                        if facts:
                            yield GraphQAStreamEvent(
                                event="facts",
                                data={
                                    "items": [
                                        item.model_dump(mode="json")
                                        for item in facts
                                    ]
                                },
                            )
                        aggregates = update.get("aggregates", [])
                        if aggregates:
                            yield GraphQAStreamEvent(
                                event="aggregates",
                                data={
                                    "items": [
                                        item.model_dump(mode="json")
                                        for item in aggregates
                                    ]
                                },
                            )
                        paths = update.get("paths", [])
                        if paths:
                            yield GraphQAStreamEvent(
                                event="paths",
                                data={
                                    "items": [
                                        item.model_dump(mode="json")
                                        for item in paths
                                    ]
                                },
                            )
                        for warning in update.get("warnings", []):
                            yield GraphQAStreamEvent(
                                event="warning",
                                data={"message": warning},
                            )
                elif mode == "custom":
                    if (
                        isinstance(payload, dict)
                        and isinstance(payload.get("event"), str)
                        and isinstance(payload.get("data"), dict)
                    ):
                        yield GraphQAStreamEvent.model_validate(payload)
                elif mode == "values":
                    final_state = payload
        except Exception:
            response = self._internal_error_response(
                request_id,
                question,
                session_id,
            )
            yield GraphQAStreamEvent(
                event="error",
                data=response.error.model_dump(mode="json"),
            )
            yield GraphQAStreamEvent(
                event="done",
                data={"response": response.model_dump(mode="json")},
            )
            return

        response = self._response_from_state(
            request_id,
            question,
            session_id,
            final_state,
        )
        if response.error is not None:
            yield GraphQAStreamEvent(
                event="error",
                data=response.error.model_dump(mode="json"),
            )
        yield GraphQAStreamEvent(
            event="done",
            data={"response": response.model_dump(mode="json")},
        )
