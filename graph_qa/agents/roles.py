# -*- coding: utf-8 -*-
"""模型调用与工作流角色的独立封装。"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from graph_qa.agents.base import AgentDescriptor, AgentRegistry
from graph_qa.agents.evidence_audit import EvidenceAuditAgent
from graph_qa.agents.retrieval import RetrievalAgent
from graph_qa.models import (
    AttributeFact,
    ConversationTurn,
    EntityCandidate,
    Evidence,
    GraphAnswerDraft,
    IntentResult,
    LinkedEntity,
    RewriteResult,
)
from graph_qa.schema import GraphSchema


@dataclass
class DialogueAgent:
    reasoner: object
    descriptor: AgentDescriptor = field(
        default=AgentDescriptor(
            name="dialogue",
            responsibility="多轮指代、规范化与上下文改写",
            stages=("normalize", "rewrite_question"),
            tools=("conversation_history",),
        ),
        init=False,
    )

    def rewrite_question(
        self,
        question: str,
        history: Sequence[ConversationTurn],
    ) -> RewriteResult:
        return self.reasoner.rewrite_question(question, history)


@dataclass
class IntentAgent:
    reasoner: object
    descriptor: AgentDescriptor = field(
        default=AgentDescriptor(
            name="intent",
            responsibility="判断问题是否属于当前图谱范围",
            stages=("prelink_entities", "classify_intent"),
        ),
        init=False,
    )

    def classify(self, question: str) -> IntentResult:
        return self.reasoner.classify_intent(question)


@dataclass
class EntityAgent:
    reasoner: object
    descriptor: AgentDescriptor = field(
        default=AgentDescriptor(
            name="entity",
            responsibility="实体候选抽取与图谱实体边界确认",
            stages=("extract_entities", "link_entities"),
            tools=("exact_linker", "fuzzy_linker", "faiss"),
        ),
        init=False,
    )

    def extract(self, question: str) -> Sequence[EntityCandidate]:
        return self.reasoner.extract_entities(question)


@dataclass
class AnswerAgent:
    reasoner: object
    descriptor: AgentDescriptor = field(
        default=AgentDescriptor(
            name="answer",
            responsibility="基于图谱上下文组织自然语言回答",
            stages=("answer_from_graph", "direct_answer"),
        ),
        init=False,
    )

    def stream_direct_answer(
        self,
        question: str,
        on_token: Callable[[str], None],
    ) -> Optional[str]:
        method = getattr(self.reasoner, "stream_direct_answer", None)
        return method(question, on_token) if callable(method) else None

    def direct_answer(self, question: str) -> str:
        return self.reasoner.direct_answer(question)

    def graph_answer(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact],
    ) -> GraphAnswerDraft:
        return self.reasoner.answer_from_graph(
            question,
            linked_entities,
            rows,
            evidence,
            facts,
        )


@dataclass
class QAAgentTeam:
    reasoner: object
    schema: GraphSchema
    max_results: int = 50
    dialogue: DialogueAgent = field(init=False)
    intent: IntentAgent = field(init=False)
    entity: EntityAgent = field(init=False)
    retrieval: RetrievalAgent = field(init=False)
    evidence_audit: EvidenceAuditAgent = field(init=False)
    answer: AnswerAgent = field(init=False)
    registry: AgentRegistry = field(init=False)

    def __post_init__(self) -> None:
        self.dialogue = DialogueAgent(self.reasoner)
        self.intent = IntentAgent(self.reasoner)
        self.entity = EntityAgent(self.reasoner)
        self.retrieval = RetrievalAgent(
            reasoner=self.reasoner,
            schema=self.schema,
            max_results=self.max_results,
        )
        self.evidence_audit = EvidenceAuditAgent()
        self.answer = AnswerAgent(self.reasoner)
        self.registry = AgentRegistry(
            [
                self.dialogue.descriptor,
                self.intent.descriptor,
                self.entity.descriptor,
                self.retrieval.descriptor,
                self.answer.descriptor,
                self.evidence_audit.descriptor,
            ]
        )

    def agent_for_stage(self, stage: str) -> AgentDescriptor:
        return self.registry.for_stage(stage)
