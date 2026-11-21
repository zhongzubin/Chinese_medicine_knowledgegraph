# -*- coding: utf-8 -*-
"""V2 API 使用的问答应用服务。"""

from typing import Iterator, Optional, Protocol, Sequence

from graph_qa.models import (
    ConversationTurn,
    GraphQAResponse,
    GraphQAStreamEvent,
)


class QAEngine(Protocol):
    def answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[Sequence[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> GraphQAResponse:
        ...

    def stream_answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[Sequence[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> Iterator[GraphQAStreamEvent]:
        ...


class QAApplicationService:
    """保持 V1 工作流不变，为 V2 提供稳定的应用层入口。"""

    def __init__(self, engine: QAEngine):
        self.engine = engine

    def answer(
        self,
        question: str,
        *,
        request_id: str,
        session_id: str,
        history: Sequence[ConversationTurn] = (),
    ) -> GraphQAResponse:
        return self.engine.answer(
            question,
            request_id=request_id,
            conversation_history=list(history),
            session_id=session_id,
        )

    def stream_answer(
        self,
        question: str,
        *,
        request_id: str,
        session_id: str,
        history: Sequence[ConversationTurn] = (),
    ) -> Iterator[GraphQAStreamEvent]:
        stream_method = getattr(self.engine, "stream_answer", None)
        if callable(stream_method):
            return stream_method(
                question,
                request_id=request_id,
                conversation_history=list(history),
                session_id=session_id,
            )
        response = self.answer(
            question,
            request_id=request_id,
            session_id=session_id,
            history=history,
        )
        events = []
        if response.answer:
            events.append(
                GraphQAStreamEvent(
                    event="answer_delta",
                    data={"delta": response.answer},
                )
            )
        if response.error is not None:
            events.append(
                GraphQAStreamEvent(
                    event="error",
                    data=response.error.model_dump(mode="json"),
                )
            )
        events.append(
            GraphQAStreamEvent(
                event="done",
                data={"response": response.model_dump(mode="json")},
            )
        )
        return iter(events)
