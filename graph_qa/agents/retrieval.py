# -*- coding: utf-8 -*-
"""受控图谱检索规划 Agent。"""

from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from graph_qa.agents.base import AgentDescriptor
from graph_qa.llm import ReasonerError
from graph_qa.models import (
    AggregateQueryPlan,
    AttributeQueryPlan,
    AttributeSelection,
    CypherPlan,
    EntityLabel,
    ErrorCode,
    LinkedEntity,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    MultiHopStep,
    RetrievalPlanningInput,
    RetrievalPlanningResult,
    UsedEntityRef,
)
from graph_qa.schema import GraphSchema

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
COUNT_TERMS = ("多少", "几个", "几种", "数量", "多少个", "多少种")
ENTITY_LABEL_DISPLAY_NAMES = {
    "Herb": "药材",
    "Formula": "方剂",
    "Disease": "疾病",
    "Symptom": "症状",
    "Effect": "功效",
    "Source": "文献",
}
MULTI_HOP_MAX_STEPS = 3
MULTI_HOP_CATALOG: Tuple[
    Tuple[
        Tuple[EntityLabel, str, EntityLabel],
        Tuple[EntityLabel, str, EntityLabel],
        str,
    ],
    ...,
] = (
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
RETRIEVAL_PLAN_LABELS: Mapping[str, str] = {
    "controlled_multi_hop": "受控多跳",
    "controlled_attribute": "图谱属性",
    "controlled_aggregate": "受控统计",
    "controlled_multi_condition_list": "多条件列表",
    "controlled_relationship": "受控关系",
    "schema_constrained_cypher": "Schema 约束查询",
    "rejected": "规划校验未通过",
}


@dataclass(frozen=True)
class RelationshipSelection:
    subject_label: EntityLabel
    relationship: str
    object_label: EntityLabel
    direction: str
    entities: Tuple[LinkedEntity, ...]


@dataclass(frozen=True)
class RetrievalLoopObservation:
    step: int
    max_steps: int
    plan_kind: Optional[str]
    is_valid: bool
    error_code: Optional[str]
    attribute_relation_fallback: bool
    has_context: bool
    row_count: int


@dataclass(frozen=True)
class RetrievalLoopDecision:
    next_action: Literal[
        "replan",
        "answer_from_graph",
        "no_evidence",
        "safe_fail",
        "finish",
    ]
    reason_code: str


def requested_attributes(question: str) -> List[str]:
    requested: List[str] = []
    for terms, attributes in ATTRIBUTE_TERM_MAP:
        if not any(term in question for term in terms):
            continue
        for attribute in attributes:
            if attribute not in requested:
                requested.append(attribute)
    return requested


def has_tcm_query_cue(question: str) -> bool:
    """识别明确的属性、关系或实体范围问法。"""
    return bool(
        requested_attributes(question)
        or _requested_relations(question)
        or _entity_hints(question)
    )


def _requested_relations(question: str) -> Set[str]:
    relations = {
        relation
        for terms, relation in RELATION_TERM_MAP
        if any(term in question for term in terms)
    }
    if (
        any(term in question for term in ("缓解", "改善"))
        and "HAS_SYMPTOM" in relations
    ):
        relations.discard("HAS_SYMPTOM")
    return relations


def _entity_hints(question: str) -> Set[str]:
    return {
        label
        for terms, label in ENTITY_HINT_MAP
        if any(term in question for term in terms)
    }


def _relation_positions(question: str) -> Dict[str, int]:
    positions = {}
    for terms, relation in RELATION_TERM_MAP:
        matches = [
            question.find(term)
            for term in terms
            if term in question
        ]
        if matches:
            positions[relation] = min(matches)
    return positions


def _target_label_hint(question: str) -> Optional[str]:
    candidates = []
    for terms, label in ENTITY_HINT_MAP:
        for term in terms:
            position = question.rfind(term)
            if position >= 0:
                candidates.append((position, len(term), label))
    if not candidates:
        return None
    return max(candidates)[2]


def _schema_paths(
    schema: GraphSchema,
    start_label: str,
    *,
    max_steps: int,
) -> List[Tuple[MultiHopStep, ...]]:
    patterns_by_subject: Dict[str, List[Tuple[str, str, str]]] = {}
    for pattern in sorted(schema.triple_patterns):
        patterns_by_subject.setdefault(pattern[0], []).append(pattern)

    results: List[Tuple[MultiHopStep, ...]] = []

    def visit(
        current_label: str,
        visited_labels: Tuple[str, ...],
        used_relations: Set[str],
        steps: Tuple[MultiHopStep, ...],
    ) -> None:
        for subject, relationship, object_label in patterns_by_subject.get(
            current_label,
            [],
        ):
            if (
                object_label in visited_labels
                or relationship in used_relations
            ):
                continue
            step = MultiHopStep(
                subject_label=subject,
                relationship=relationship,
                object_label=object_label,
            )
            next_steps = (*steps, step)
            if len(next_steps) >= 2:
                results.append(next_steps)
            if len(next_steps) < max_steps:
                visit(
                    object_label,
                    (*visited_labels, object_label),
                    {*used_relations, relationship},
                    next_steps,
                )

    visit(start_label, (start_label,), set(), ())
    return results


def _multi_hop_description(
    anchor_names: Sequence[str],
    steps: Sequence[MultiHopStep],
) -> str:
    suffix = {
        "HAS_EFFECT": "关联的功效",
        "TREATS_DISEASE": "可治疗的疾病",
        "ALLEVIATES_SYMPTOM": "可缓解的症状",
        "HAS_SYMPTOM": "治疗疾病的相关症状",
        "FROM_SOURCE": "相关来源",
        "HAS_INGREDIENT": "包含的成分",
    }.get(steps[-1].relationship, "关联结果")
    return f"{'、'.join(anchor_names)}{suffix}"


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
    linked_entities: Sequence[LinkedEntity],
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


def _parameter_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


@dataclass
class RetrievalAgent:
    reasoner: object
    schema: GraphSchema
    max_results: int = 50
    dynamic_multi_hop_enabled: bool = True
    descriptor: AgentDescriptor = field(
        default=AgentDescriptor(
            name="retrieval",
            responsibility=(
                "受控查询规划与结果观察；"
                "安全校验和只读执行由确定性层承担"
            ),
            stages=(
                "plan_cypher",
                "validate_cypher",
                "execute_cypher",
                "retrieve_observe",
            ),
            tools=(
                "controlled_query_catalog",
                "schema_constrained_text2cypher",
            ),
        ),
        init=False,
    )

    def __post_init__(self) -> None:
        if self.max_results < 1:
            raise ValueError("max_results 必须大于等于 1")

    def plan(
        self,
        request: RetrievalPlanningInput,
    ) -> RetrievalPlanningResult:
        requested_relations = _requested_relations(request.question)
        if len(requested_relations) > MULTI_HOP_MAX_STEPS:
            return RetrievalPlanningResult(
                plan_kind="rejected",
                query_attempts=request.query_attempts,
                validation_errors=["多跳路径超过 3 步上限"],
                last_error_code=ErrorCode.CYPHER_REJECTED,
            )
        multi_hop_plan = self._build_multi_hop_plan(request)
        if multi_hop_plan is not None:
            return self._controlled_result(
                "controlled_multi_hop",
                multi_hop_plan,
                request,
            )
        if self.dynamic_multi_hop_enabled and len(requested_relations) >= 2:
            return RetrievalPlanningResult(
                plan_kind="rejected",
                query_attempts=request.query_attempts,
                validation_errors=["问题包含多个关系，但没有唯一安全多跳路径"],
                last_error_code=ErrorCode.CYPHER_REJECTED,
            )

        attribute_plan = self._build_attribute_plan(request)
        if attribute_plan is not None:
            return self._controlled_result(
                "controlled_attribute",
                attribute_plan,
                request,
            )

        aggregate_plan = self._build_aggregate_plan(request)
        if aggregate_plan is not None:
            return self._controlled_result(
                "controlled_aggregate",
                aggregate_plan,
                request,
            )

        multi_condition_plan = self._build_multi_condition_list_plan(request)
        if multi_condition_plan is not None:
            return self._controlled_result(
                "controlled_multi_condition_list",
                multi_condition_plan,
                request,
            )

        template_plan = self._build_relationship_template_plan(request)
        if template_plan is not None:
            return self._controlled_result(
                "controlled_relationship",
                template_plan,
                request,
            )

        return self._generate_schema_constrained_cypher(request)

    def observe(
        self,
        observation: RetrievalLoopObservation,
    ) -> RetrievalLoopDecision:
        """根据本轮工具结果决定继续规划还是结束。"""
        if observation.error_code:
            return RetrievalLoopDecision(
                next_action="finish",
                reason_code="EXECUTION_ERROR",
            )
        if not observation.is_valid:
            if observation.step < observation.max_steps:
                return RetrievalLoopDecision(
                    next_action="replan",
                    reason_code="VALIDATION_RETRY",
                )
            return RetrievalLoopDecision(
                next_action="safe_fail",
                reason_code="VALIDATION_RETRIES_EXHAUSTED",
            )
        if observation.attribute_relation_fallback:
            if observation.step < observation.max_steps:
                return RetrievalLoopDecision(
                    next_action="replan",
                    reason_code="ATTRIBUTE_EMPTY_RELATION_FALLBACK",
                )
            return RetrievalLoopDecision(
                next_action="safe_fail",
                reason_code="RETRIEVAL_STEPS_EXHAUSTED",
            )
        if observation.has_context:
            return RetrievalLoopDecision(
                next_action="answer_from_graph",
                reason_code="VERIFIABLE_CONTEXT_AVAILABLE",
            )
        return RetrievalLoopDecision(
            next_action="no_evidence",
            reason_code=(
                "QUERY_RETURNED_NO_ROWS"
                if observation.row_count == 0
                else "NO_VERIFIABLE_CONTEXT"
            ),
        )

    @staticmethod
    def _controlled_result(
        plan_kind: str,
        plan: Any,
        request: RetrievalPlanningInput,
    ) -> RetrievalPlanningResult:
        return RetrievalPlanningResult(
            plan_kind=plan_kind,
            plan=plan,
            query_attempts=request.query_attempts,
        )

    def _build_attribute_plan(
        self,
        request: RetrievalPlanningInput,
    ) -> Optional[AttributeQueryPlan]:
        if request.attribute_query_attempted:
            return None
        requested = set(requested_attributes(request.question))
        if not requested:
            return None

        used_entities: List[UsedEntityRef] = []
        selections_by_label: Dict[str, Set[str]] = {}
        for entity in request.linked_entities:
            queryable = set(self.schema.queryable_attributes(entity.label))
            selected = requested & queryable
            if not selected:
                continue
            used_entities.append(
                UsedEntityRef(label=entity.label, name=entity.name)
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
        request: RetrievalPlanningInput,
    ) -> Optional[RelationshipSelection]:
        question = request.question
        requested_relations = _requested_relations(question)
        if not requested_relations:
            return None
        hints = _entity_hints(question)
        linked_by_label: Dict[str, List[LinkedEntity]] = {}
        for entity in request.linked_entities:
            linked_by_label.setdefault(entity.label, []).append(entity)

        candidates: List[Tuple[int, RelationshipSelection]] = []
        for subject_label, relation, object_label in (
            self.schema.triple_patterns
        ):
            if relation not in requested_relations:
                continue
            direction_hint = _relationship_direction_hint(
                question,
                request.linked_entities,
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
        request: RetrievalPlanningInput,
    ) -> Optional[CypherPlan]:
        selection = self._select_relationship_pattern(request)
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
        request: RetrievalPlanningInput,
    ) -> Optional[AggregateQueryPlan]:
        if not _is_count_question(request.question):
            return None
        selection = self._select_relationship_pattern(request)
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
            and _has_explicit_conjunction(request.question)
        )
        if (
            len(anchor_names) > 1
            and selection.direction == "outgoing"
            and _has_explicit_conjunction(request.question)
        ):
            return None
        return AggregateQueryPlan(
            subject_label=selection.subject_label,
            relationship=selection.relationship,
            object_label=selection.object_label,
            used_entities=[
                UsedEntityRef(label=item.label, name=item.name)
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
        request: RetrievalPlanningInput,
    ) -> Optional[MultiConditionQueryPlan]:
        if _is_count_question(request.question):
            return None
        selection = self._select_relationship_pattern(request)
        if selection is None or selection.direction != "incoming":
            return None
        anchor_names = sorted(
            {item.name for item in selection.entities}
        )
        if (
            len(anchor_names) < 2
            or not _has_explicit_conjunction(request.question)
        ):
            return None
        return MultiConditionQueryPlan(
            subject_label=selection.subject_label,
            relationship=selection.relationship,
            object_label=selection.object_label,
            used_entities=[
                UsedEntityRef(label=item.label, name=item.name)
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
        request: RetrievalPlanningInput,
    ) -> Optional[MultiHopQueryPlan]:
        if self.dynamic_multi_hop_enabled:
            return self._build_dynamic_multi_hop_plan(request)
        return self._build_fixed_two_hop_plan(request)

    def _build_dynamic_multi_hop_plan(
        self,
        request: RetrievalPlanningInput,
    ) -> Optional[MultiHopQueryPlan]:
        requested_relations = _requested_relations(request.question)
        if not 2 <= len(requested_relations) <= MULTI_HOP_MAX_STEPS:
            return None
        relation_positions = _relation_positions(request.question)
        if not requested_relations.issubset(relation_positions):
            return None

        linked_by_label: Dict[str, List[LinkedEntity]] = {}
        for entity in request.linked_entities:
            linked_by_label.setdefault(entity.label, []).append(entity)
        if not linked_by_label:
            return None

        target_hint = _target_label_hint(request.question)
        candidates = {}
        for start_label in sorted(linked_by_label):
            for steps in _schema_paths(
                self.schema,
                start_label,
                max_steps=MULTI_HOP_MAX_STEPS,
            ):
                relationships = [step.relationship for step in steps]
                if set(relationships) != requested_relations:
                    continue
                positions = [
                    relation_positions[relationship]
                    for relationship in relationships
                ]
                if any(
                    previous >= current
                    for previous, current in zip(positions, positions[1:])
                ):
                    continue
                if target_hint and steps[-1].object_label != target_hint:
                    continue
                key = (
                    start_label,
                    tuple(
                        (
                            step.subject_label,
                            step.relationship,
                            step.object_label,
                        )
                        for step in steps
                    ),
                )
                candidates[key] = steps

        if len(candidates) != 1:
            return None
        (start_label, _steps_key), steps = next(iter(candidates.items()))
        entities = linked_by_label[start_label]
        anchor_names = sorted({item.name for item in entities})
        return MultiHopQueryPlan(
            steps=list(steps),
            used_entities=[
                UsedEntityRef(label=item.label, name=item.name)
                for item in sorted(
                    entities,
                    key=lambda entity: (entity.label, entity.name),
                )
            ],
            description=_multi_hop_description(anchor_names, steps),
        )

    def _build_fixed_two_hop_plan(
        self,
        request: RetrievalPlanningInput,
    ) -> Optional[MultiHopQueryPlan]:
        requested_relations = _requested_relations(request.question)
        if len(requested_relations) < 2:
            return None
        linked_by_label: Dict[str, List[LinkedEntity]] = {}
        for entity in request.linked_entities:
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
            candidates.append(
                (first_step, second_step, result_description, entities)
            )
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
                UsedEntityRef(label=item.label, name=item.name)
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
        target_entities: Sequence[LinkedEntity],
        *,
        reverse: bool,
    ) -> CypherPlan:
        parameter_target = "object" if reverse else "subject"
        limit = min(20, self.max_results)
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
                UsedEntityRef(label=entity.label, name=entity.name)
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

    def _generate_schema_constrained_cypher(
        self,
        request: RetrievalPlanningInput,
    ) -> RetrievalPlanningResult:
        attempts = request.query_attempts + 1
        try:
            plan = self.reasoner.generate_cypher(
                request.question,
                request.linked_entities,
                self.schema,
                request.validation_errors,
            )
            linked_keys = {
                (item.label, item.name)
                for item in request.linked_entities
            }
            used_keys = {
                (item.label, item.name)
                for item in plan.used_entities
            }
            if not used_keys or not used_keys.issubset(linked_keys):
                return RetrievalPlanningResult(
                    plan_kind="rejected",
                    query_attempts=attempts,
                    validation_errors=["模型引用了未链接的实体"],
                    last_error_code=ErrorCode.CYPHER_REJECTED,
                )
            parameter_names = {
                value
                for parameter_value in plan.parameters.values()
                for value in _parameter_strings(parameter_value)
            }
            if not any(
                item.name in parameter_names
                for item in request.linked_entities
            ):
                return RetrievalPlanningResult(
                    plan_kind="rejected",
                    query_attempts=attempts,
                    validation_errors=["查询参数没有包含已链接实体名称"],
                    last_error_code=ErrorCode.CYPHER_REJECTED,
                )
            return RetrievalPlanningResult(
                plan_kind="schema_constrained_cypher",
                plan=plan,
                query_attempts=attempts,
            )
        except ReasonerError as exc:
            return RetrievalPlanningResult(
                plan_kind="rejected",
                query_attempts=attempts,
                validation_errors=[exc.message],
                last_error_code=exc.code,
            )
        except Exception:
            return RetrievalPlanningResult(
                plan_kind="rejected",
                query_attempts=attempts,
                validation_errors=["生成查询时发生内部错误"],
                last_error_code=ErrorCode.INTERNAL_ERROR,
            )
