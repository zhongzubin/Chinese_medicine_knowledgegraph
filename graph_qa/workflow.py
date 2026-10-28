# -*- coding: utf-8 -*-
"""可验证的中医图谱问答 LangGraph 工作流。"""

import re
import unicodedata
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from graph_qa.cypher_guard import CypherGuard, CypherGuardError, detect_unsafe_question
from graph_qa.entity_linker import EntityIndex
from graph_qa.llm import QAReasoner, ReasonerError
from graph_qa.models import (
    ConversationTurn,
    CypherPlan,
    ErrorCode,
    Evidence,
    GraphQAStreamEvent,
    GraphQARequest,
    GraphQAResponse,
    IntentResult,
    QueryInfo,
    append_disclaimer,
)
from graph_qa.neo4j_repository import RepositoryError
from graph_qa.schema import GraphSchema
from graph_qa.state import AgentState

WHITESPACE_RE = re.compile(r"\s+")

NO_EVIDENCE_ANSWER = "当前图谱没有找到足以回答该问题的依据。"
CLARIFY_ANSWER = (
    "我暂时没有在知识图谱中匹配到具体实体。"
    "请补充药材、方剂、疾病、症状、功效或文献名称后再试。"
)
SAFE_RETRY_ANSWER = "未能生成符合只读安全规则的图谱查询，请换一种更具体的问法。"

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
    evidence: List[Evidence] = []
    seen: Set[Tuple[str, str, str, str, str, Optional[str]]] = set()
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
            normalized_source,
        )
        if key in seen:
            continue
        seen.add(key)
        evidence.append(
            Evidence(
                subject=subject.strip(),
                subject_label=subject_label,
                relationship=relationship,
                object=object_name.strip(),
                object_label=object_label,
                source=normalized_source,
            )
        )
    return evidence


def _evidence_fallback_answer(evidence: Sequence[Evidence]) -> str:
    lines = ["根据当前图谱中可验证的关系："]
    for item in evidence[:5]:
        source = f"（来源：{item.source}）" if item.source else ""
        lines.append(
            f"- {item.subject} 通过 {item.relationship} 关联到 {item.object}{source}"
        )
    return "\n".join(lines)


def _parameter_strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _active_question(state: AgentState) -> str:
    return state.get("rewritten_question") or state["normalized_question"]


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
                "finish": "finish",
            },
        )

        for terminal_node in (
            "answer_from_graph",
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
        if not history:
            return {
                "rewritten_question": question,
                "stage_events": ["rewrite_question"],
            }

        rewrite = getattr(self.reasoner, "rewrite_question", None)
        if not callable(rewrite):
            return {
                "rewritten_question": question,
                "stage_events": ["rewrite_question"],
            }
        try:
            result = rewrite(question, history)
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
            intent = self.reasoner.classify_intent(_active_question(state))
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
            candidates = self.reasoner.extract_entities(_active_question(state))
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

    def _plan_cypher(self, state: AgentState) -> Dict[str, Any]:
        attempts = state.get("query_attempts", 0) + 1
        linked_entities = state.get("linked_entities", [])
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
        update: Dict[str, Any] = {
            "query_result": result,
            "evidence": evidence,
            "stage_events": ["execute_cypher"],
        }
        if result.rows and not evidence:
            update.update(
                {
                    "error_code": ErrorCode.NO_GRAPH_EVIDENCE.value,
                    "error_message": "查询返回了数据，但没有可验证的证据字段",
                    "answer": NO_EVIDENCE_ANSWER,
                    "grounded": False,
                }
            )
        return update

    def _answer_from_graph(self, state: AgentState) -> Dict[str, Any]:
        evidence = state.get("evidence", [])
        query_result = state.get("query_result")
        if not evidence or query_result is None:
            return self._terminal_error(
                state,
                ErrorCode.NO_GRAPH_EVIDENCE,
                "没有可验证的图谱证据",
                NO_EVIDENCE_ANSWER,
                "answer_from_graph",
            )
        parts: List[str] = []

        def on_token(token: str) -> None:
            if token:
                parts.append(token)
                _emit_answer_delta(token)

        stream_answer = getattr(self.reasoner, "stream_answer_from_graph", None)
        try:
            if callable(stream_answer):
                streamed = stream_answer(
                    _active_question(state),
                    state.get("linked_entities", []),
                    query_result.rows,
                    evidence,
                    on_token,
                )
                answer = "".join(parts) or streamed
            else:
                answer = self.reasoner.answer_from_graph(
                    _active_question(state),
                    state.get("linked_entities", []),
                    query_result.rows,
                    evidence,
                )
                _emit_answer_delta(answer)
            final_answer = append_disclaimer(answer)
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
                final_answer = append_disclaimer("".join(parts))
                _emit_answer_delta(final_answer[len("".join(parts)) :])
                return {
                    "answer": final_answer,
                    "grounded": True,
                    "warnings": ["回答流式生成中断，内容可能不完整。"],
                    "stage_events": ["answer_from_graph"],
                }
            fallback = append_disclaimer(_evidence_fallback_answer(evidence))
            _emit_answer_delta(fallback)
            return {
                "answer": fallback,
                "grounded": True,
                "warnings": ["回答生成模型不可用，已使用图谱证据生成简化回答。"],
                "stage_events": ["answer_from_graph"],
            }

    def _direct_answer(self, state: AgentState) -> Dict[str, Any]:
        parts: List[str] = []

        def on_token(token: str) -> None:
            if token:
                parts.append(token)
                _emit_answer_delta(token)

        stream_answer = getattr(self.reasoner, "stream_direct_answer", None)
        try:
            if callable(stream_answer):
                streamed = stream_answer(_active_question(state), on_token)
                answer = "".join(parts) or streamed
            else:
                answer = self.reasoner.direct_answer(_active_question(state))
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
        linked = self.entity_index.match(
            [],
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
        result = state.get("query_result")
        if result is None or result.row_count == 0:
            return "no_evidence"
        return "answer_from_graph"

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
            "cypher_plan": None,
            "query_result": None,
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
                        yield GraphQAStreamEvent(
                            event="stage",
                            data={
                                "stage": node_name,
                                "label": STAGE_LABELS.get(
                                    node_name,
                                    node_name,
                                ),
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
