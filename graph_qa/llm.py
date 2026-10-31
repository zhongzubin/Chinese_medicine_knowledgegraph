# -*- coding: utf-8 -*-
"""大模型适配器和结构化输出解析。"""

import json
from typing import Any, Callable, List, Protocol, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.config import Config
from graph_qa.models import (
    AttributeFact,
    ConversationTurn,
    CypherPlan,
    EntityCandidate,
    ErrorCode,
    Evidence,
    IntentResult,
    LinkedEntity,
    RewriteResult,
)
from graph_qa.prompts import (
    SAFETY_SYSTEM_PROMPT,
    cypher_generation_prompt,
    direct_answer_prompt,
    entity_extraction_prompt,
    graph_answer_prompt,
    intent_prompt,
    rewrite_question_prompt,
)
from graph_qa.schema import GraphSchema


class ReasonerError(RuntimeError):
    """表示模型不可用或返回内容不符合结构化契约。"""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.MODEL_UNAVAILABLE):
        super().__init__(message)
        self.code = code
        self.message = message


class QAReasoner(Protocol):
    def rewrite_question(
        self,
        question: str,
        history: Sequence[ConversationTurn],
    ) -> RewriteResult:
        ...

    def classify_intent(self, question: str) -> IntentResult:
        ...

    def extract_entities(self, question: str) -> List[EntityCandidate]:
        ...

    def generate_cypher(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        schema: GraphSchema,
        previous_errors: Sequence[str],
    ) -> CypherPlan:
        ...

    def direct_answer(self, question: str) -> str:
        ...

    def stream_direct_answer(
        self,
        question: str,
        on_token: Callable[[str], None],
    ) -> str:
        ...

    def answer_from_graph(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact] = (),
    ) -> str:
        ...

    def stream_answer_from_graph(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact],
        on_token: Callable[[str], None],
    ) -> str:
        ...


class _EntityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: List[EntityCandidate] = Field(default_factory=list)


def _message_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return str(content).strip()


class LangChainQAReasoner:
    def __init__(self, llm: Any = None):
        if llm is None:
            config = Config()
            missing = [
                name
                for name, value in {
                    "MODEL_API_KEY": config.MODEL_API_KEY,
                    "MODEL_BASE_URL": config.MODEL_BASE_URL,
                    "MODEL_NAME": config.MODEL_NAME,
                }.items()
                if not value
            ]
            if missing:
                raise ReasonerError(
                    f"缺少模型配置: {', '.join(missing)}",
                    ErrorCode.CONFIG_ERROR,
                )
            llm = ChatOpenAI(
                api_key=config.MODEL_API_KEY,
                base_url=config.MODEL_BASE_URL,
                model=config.MODEL_NAME,
                temperature=0,
                timeout=60,
                max_retries=2,
            )
        self.llm = llm

    def _invoke_json(self, prompt: str, model_type: type[BaseModel]) -> BaseModel:
        try:
            response = self.llm.invoke(
                [
                    SystemMessage(content=SAFETY_SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ]
            )
            raw = _message_text(response)
            payload = json.loads(raw)
            return model_type.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ReasonerError(
                "模型返回内容不符合结构化格式",
                ErrorCode.MODEL_OUTPUT_INVALID,
            ) from exc
        except Exception as exc:
            raise ReasonerError("模型调用失败", ErrorCode.MODEL_UNAVAILABLE) from exc

    def classify_intent(self, question: str) -> IntentResult:
        result = self._invoke_json(intent_prompt(question), IntentResult)
        return result  # type: ignore[return-value]

    def rewrite_question(
        self,
        question: str,
        history: Sequence[ConversationTurn],
    ) -> RewriteResult:
        result = self._invoke_json(
            rewrite_question_prompt(question, history),
            RewriteResult,
        )
        return result  # type: ignore[return-value]

    def extract_entities(self, question: str) -> List[EntityCandidate]:
        result = self._invoke_json(entity_extraction_prompt(question), _EntityOutput)
        return result.candidates  # type: ignore[attr-defined]

    def generate_cypher(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        schema: GraphSchema,
        previous_errors: Sequence[str],
    ) -> CypherPlan:
        result = self._invoke_json(
            cypher_generation_prompt(
                question,
                linked_entities,
                schema,
                previous_errors,
            ),
            CypherPlan,
        )
        return result  # type: ignore[return-value]

    def _invoke_text(self, prompt: str) -> str:
        try:
            response = self.llm.invoke(
                [
                    SystemMessage(content=SAFETY_SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ]
            )
            text = _message_text(response)
            if not text:
                raise ReasonerError(
                    "模型返回空答案",
                    ErrorCode.MODEL_OUTPUT_INVALID,
                )
            return text
        except ReasonerError:
            raise
        except Exception as exc:
            raise ReasonerError("模型调用失败", ErrorCode.MODEL_UNAVAILABLE) from exc

    def _stream_text(
        self,
        prompt: str,
        on_token: Callable[[str], None],
    ) -> str:
        parts = []
        try:
            for chunk in self.llm.stream(
                [
                    SystemMessage(content=SAFETY_SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ]
            ):
                text = _message_text(chunk)
                if text:
                    parts.append(text)
                    on_token(text)
            if not parts:
                raise ReasonerError(
                    "模型返回空答案",
                    ErrorCode.MODEL_OUTPUT_INVALID,
                )
            return "".join(parts)
        except ReasonerError:
            raise
        except Exception as exc:
            raise ReasonerError("模型调用失败", ErrorCode.MODEL_UNAVAILABLE) from exc

    def direct_answer(self, question: str) -> str:
        return self._invoke_text(direct_answer_prompt(question))

    def stream_direct_answer(
        self,
        question: str,
        on_token: Callable[[str], None],
    ) -> str:
        return self._stream_text(direct_answer_prompt(question), on_token)

    def answer_from_graph(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact] = (),
    ) -> str:
        evidence_rows: List[dict] = [
            item.model_dump(mode="json") for item in evidence
        ]
        return self._invoke_text(
            graph_answer_prompt(
                question,
                linked_entities,
                rows,
                evidence_rows,
                facts,
            )
        )

    def stream_answer_from_graph(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact],
        on_token: Callable[[str], None],
    ) -> str:
        evidence_rows: List[dict] = [
            item.model_dump(mode="json") for item in evidence
        ]
        return self._stream_text(
            graph_answer_prompt(
                question,
                linked_entities,
                rows,
                evidence_rows,
                facts,
            ),
            on_token,
        )
