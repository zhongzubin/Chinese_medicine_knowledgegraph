# -*- coding: utf-8 -*-
"""中医知识图谱问答 FastAPI 服务。"""

import json
import logging
import uuid
import uvicorn
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable, Iterator, Literal, Optional, Protocol
from uuid import UUID

from fastapi import FastAPI, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader

from graph_qa.api_security import (
    APIConfigurationError,
    APIKeyAuthenticator,
    APISettings,
    AuthenticationError,
)
from graph_qa.cli import build_engine_from_environment
from graph_qa.models import (
    APIQARequest,
    ConversationTurn,
    ErrorCode,
    ErrorInfo,
    GraphQAResponse,
    GraphQAStreamEvent,
    StrictModel,
)
from graph_qa.redis_limits import (
    ConcurrencyHeartbeat,
    LimitStore,
    RedisLimitStore,
)
from graph_qa.session_store import (
    ConversationStore,
    RedisConversationStore,
    SessionStoreError,
)

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="由部署环境配置的问答服务 API Key",
)


class QAEngine(Protocol):
    repository: Any

    def answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[list[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> GraphQAResponse:
        ...

    def stream_answer(
        self,
        question: str,
        request_id: Optional[str] = None,
        conversation_history: Optional[list[ConversationTurn]] = None,
        session_id: Optional[str] = None,
    ) -> Iterator[GraphQAStreamEvent]:
        ...


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    schema_version: Literal["1.0"] = "1.0"


EngineFactory = Callable[[APISettings], QAEngine]
LimitStoreFactory = Callable[[APISettings], LimitStore]
SessionStoreFactory = Callable[[APISettings], ConversationStore]


def _default_engine_factory(settings: APISettings) -> QAEngine:
    return build_engine_from_environment(
        database=settings.database,
        max_results=settings.max_results,
    )


def _default_limit_store_factory(settings: APISettings) -> LimitStore:
    return RedisLimitStore(settings)


def _default_session_store_factory(
    settings: APISettings,
) -> ConversationStore:
    return RedisConversationStore(settings)


def _close_quietly(value: Any) -> None:
    close = getattr(value, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:
        logger.warning("API 资源关闭失败: %s", type(exc).__name__)


def _error_response(
    request_id: str,
    question: str,
    answer: str,
    code: ErrorCode,
    message: str,
    status_code: int,
    headers: Optional[dict[str, str]] = None,
    session_id: Optional[str] = None,
) -> JSONResponse:
    response = GraphQAResponse(
        request_id=request_id,
        question=question,
        answer=answer,
        grounded=False,
        error=ErrorInfo(code=code, message=message),
        session_id=session_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers=headers,
    )


def _status_for_response(response: GraphQAResponse) -> int:
    if response.error is None:
        return 200
    return {
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.UNSAFE_REQUEST: 400,
        ErrorCode.CONFIG_ERROR: 503,
        ErrorCode.MODEL_UNAVAILABLE: 503,
        ErrorCode.MODEL_OUTPUT_INVALID: 502,
        ErrorCode.CYPHER_REJECTED: 502,
        ErrorCode.CYPHER_INVALID: 502,
        ErrorCode.QUERY_TIMEOUT: 504,
        ErrorCode.QUERY_EXECUTION_FAILED: 502,
        ErrorCode.NO_GRAPH_EVIDENCE: 502,
        ErrorCode.INTERNAL_ERROR: 500,
    }.get(response.error.code, 500)


def _format_sse(event: GraphQAStreamEvent) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.event}\ndata: {payload}\n\n"


def create_app(
    settings: Optional[APISettings] = None,
    engine_factory: Optional[EngineFactory] = None,
    limit_store_factory: Optional[LimitStoreFactory] = None,
    session_store_factory: Optional[SessionStoreFactory] = None,
) -> FastAPI:
    """创建应用；依赖只在 lifespan 中初始化。"""

    engine_builder = engine_factory or _default_engine_factory
    store_builder = limit_store_factory or _default_limit_store_factory
    session_builder = session_store_factory or _default_session_store_factory

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings: Optional[APISettings] = None
        active_store: Optional[LimitStore] = None
        active_session_store: Optional[ConversationStore] = None
        active_engine: Optional[QAEngine] = None
        try:
            active_settings = settings or APISettings.from_environment()
            authenticator = APIKeyAuthenticator(active_settings)
            active_store = store_builder(active_settings)
            active_session_store = session_builder(active_settings)
            if not active_store.ping():
                raise APIConfigurationError("Redis 连接不可用")
            if not active_session_store.ping():
                raise APIConfigurationError("Redis 会话存储不可用")
            active_engine = engine_builder(active_settings)
        except APIConfigurationError:
            _close_quietly(active_store)
            _close_quietly(active_session_store)
            _close_quietly(active_engine)
            raise
        except Exception as exc:
            _close_quietly(active_store)
            _close_quietly(active_session_store)
            _close_quietly(active_engine)
            raise APIConfigurationError("问答 API 初始化失败") from None

        application.state.settings = active_settings
        application.state.authenticator = authenticator
        application.state.limit_store = active_store
        application.state.session_store = active_session_store
        application.state.engine = active_engine
        try:
            yield
        finally:
            repository = getattr(active_engine, "repository", None)
            _close_quietly(repository)
            _close_quietly(active_store)
            _close_quietly(active_session_store)

    application = FastAPI(
        title="中医知识图谱问答 API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        return _error_response(
            request_id=str(uuid.uuid4()),
            question="",
            answer="请求参数无效。",
            code=ErrorCode.INVALID_REQUEST,
            message="请求体必须包含长度为 1 到 500 的 question，且不能包含未知字段",
            status_code=422,
        )

    @application.get("/health", response_model=HealthResponse)
    def health(request: Request):
        try:
            healthy = bool(
                request.app.state.limit_store.ping()
                and request.app.state.session_store.ping()
            )
        except Exception as exc:
            logger.warning("API 健康检查失败: %s", type(exc).__name__)
            healthy = False
        if not healthy:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "schema_version": "1.0"},
            )
        return HealthResponse()

    @application.post(
        "/api/v1/qa",
        response_model=GraphQAResponse,
        responses={
            400: {"model": GraphQAResponse},
            401: {"model": GraphQAResponse},
            404: {"model": GraphQAResponse},
            409: {"model": GraphQAResponse},
            429: {"model": GraphQAResponse},
            500: {"model": GraphQAResponse},
            502: {"model": GraphQAResponse},
            503: {"model": GraphQAResponse},
            504: {"model": GraphQAResponse},
        },
    )
    def answer_question(
        payload: APIQARequest,
        request: Request,
        api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    ):
        request_id = str(uuid.uuid4())
        session_id: Optional[str] = None
        history: list[ConversationTurn] = []
        session_lock_token: Optional[str] = None
        active_settings: APISettings = request.app.state.settings
        authenticator: APIKeyAuthenticator = request.app.state.authenticator
        store: LimitStore = request.app.state.limit_store
        session_store: ConversationStore = request.app.state.session_store
        engine: QAEngine = request.app.state.engine

        try:
            client_id = authenticator.authenticate(api_key)
        except AuthenticationError:
            return _error_response(
                request_id=request_id,
                question="",
                answer="请求未获授权。",
                code=ErrorCode.UNAUTHORIZED,
                message="API Key 缺失或无效",
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )

        session_id = str(payload.session_id or uuid.uuid4())
        try:
            rate_decision = store.consume_rate_limit(client_id, request_id)
        except Exception as exc:
            logger.warning("Redis 限流检查失败: %s", type(exc).__name__)
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求限流服务不可用，请稍后重试。",
                code=ErrorCode.RATE_LIMIT_UNAVAILABLE,
                message="请求限流服务暂时不可用",
                status_code=503,
                session_id=session_id,
            )

        rate_headers = rate_decision.headers()
        if not rate_decision.allowed:
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求过于频繁，请稍后重试。",
                code=ErrorCode.RATE_LIMITED,
                message="请求超过当前客户端限流额度",
                status_code=429,
                headers=rate_headers,
                session_id=session_id,
            )

        try:
            concurrency_decision = store.acquire_concurrency(request_id)
        except Exception as exc:
            logger.warning("Redis 并发租约获取失败: %s", type(exc).__name__)
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求限流服务不可用，请稍后重试。",
                code=ErrorCode.RATE_LIMIT_UNAVAILABLE,
                message="请求限流服务暂时不可用",
                status_code=503,
                headers=rate_headers,
                session_id=session_id,
            )

        if not concurrency_decision.allowed:
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="当前问答请求较多，请稍后重试。",
                code=ErrorCode.SERVICE_BUSY,
                message="问答服务已达到全局并发上限",
                status_code=503,
                headers=rate_headers,
                session_id=session_id,
            )

        heartbeat = ConcurrencyHeartbeat(
            store=store,
            request_id=request_id,
            interval_seconds=max(
                1.0,
                active_settings.concurrency_lease_seconds / 3,
            ),
        )
        try:
            try:
                heartbeat.start()
            except Exception as exc:
                logger.error("启动并发租约续租失败: %s", type(exc).__name__)
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="问答服务暂时不可用，请稍后重试。",
                    code=ErrorCode.INTERNAL_ERROR,
                    message="问答服务初始化请求失败",
                    status_code=500,
                    headers=rate_headers,
                    session_id=session_id,
                )
            try:
                locked = session_store.acquire_lock(
                    client_id,
                    session_id,
                    request_id,
                )
            except SessionStoreError as exc:
                logger.warning("获取会话锁失败: %s", str(exc))
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="会话服务暂时不可用，请稍后重试。",
                    code=ErrorCode.SESSION_UNAVAILABLE,
                    message="会话服务暂时不可用",
                    status_code=503,
                    headers=rate_headers,
                    session_id=session_id,
                )
            if not locked:
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="该会话正在处理其他问题，请稍后重试。",
                    code=ErrorCode.SESSION_BUSY,
                    message="同一会话不能并发处理多个请求",
                    status_code=409,
                    headers=rate_headers,
                    session_id=session_id,
                )
            session_lock_token = request_id
            if payload.session_id is None:
                try:
                    session_store.save(client_id, session_id, [])
                except SessionStoreError as exc:
                    logger.warning("创建会话失败: %s", str(exc))
                    return _error_response(
                        request_id=request_id,
                        question=payload.question,
                        answer="会话服务暂时不可用，请稍后重试。",
                        code=ErrorCode.SESSION_UNAVAILABLE,
                        message="无法创建会话",
                        status_code=503,
                        headers=rate_headers,
                        session_id=session_id,
                    )
            else:
                try:
                    loaded_history = session_store.load(
                        client_id,
                        session_id,
                    )
                except SessionStoreError as exc:
                    logger.warning("读取会话历史失败: %s", str(exc))
                    return _error_response(
                        request_id=request_id,
                        question=payload.question,
                        answer="会话服务暂时不可用，请稍后重试。",
                        code=ErrorCode.SESSION_UNAVAILABLE,
                        message="会话服务暂时不可用",
                        status_code=503,
                        headers=rate_headers,
                        session_id=session_id,
                    )
                if loaded_history is None:
                    return _error_response(
                        request_id=request_id,
                        question=payload.question,
                        answer="会话不存在或已过期。",
                        code=ErrorCode.SESSION_NOT_FOUND,
                        message="未找到可用的会话",
                        status_code=404,
                        headers=rate_headers,
                        session_id=session_id,
                    )
                history = loaded_history

            try:
                response = engine.answer(
                    payload.question,
                    request_id=request_id,
                    conversation_history=history,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.error("问答 API 内部处理失败: %s", type(exc).__name__)
                response = GraphQAResponse(
                    request_id=request_id,
                    question=payload.question,
                    answer="系统处理问题失败，请稍后重试。",
                    grounded=False,
                    error=ErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR,
                        message="问答处理发生内部错误",
                    ),
                    session_id=session_id,
                )

            if response.error is None:
                turn = ConversationTurn(
                    question=payload.question,
                    answer=response.answer,
                    linked_entities=response.linked_entities,
                    grounded=response.grounded,
                )
                try:
                    session_store.save(
                        client_id,
                        session_id,
                        [*history, turn],
                    )
                except SessionStoreError as exc:
                    logger.warning("保存会话历史失败: %s", str(exc))
                    return _error_response(
                        request_id=request_id,
                        question=payload.question,
                        answer="会话服务暂时不可用，请稍后重试。",
                        code=ErrorCode.SESSION_UNAVAILABLE,
                        message="本轮回答未能保存到会话",
                        status_code=503,
                        headers=rate_headers,
                        session_id=session_id,
                    )

            if response.session_id != session_id:
                response = response.model_copy(update={"session_id": session_id})
            return JSONResponse(
                status_code=_status_for_response(response),
                content=response.model_dump(mode="json"),
                headers=rate_headers,
            )
        finally:
            if session_lock_token is not None:
                try:
                    session_store.release_lock(
                        client_id,
                        session_id,
                        session_lock_token,
                    )
                except SessionStoreError as exc:
                    logger.warning("释放会话锁失败: %s", str(exc))
            heartbeat.stop()
            try:
                store.release_concurrency(request_id)
            except Exception as exc:
                logger.warning("Redis 并发租约释放失败: %s", type(exc).__name__)

    @application.post(
        "/api/v1/qa/stream",
        responses={
            200: {"content": {"text/event-stream": {}}},
            400: {"model": GraphQAResponse},
            401: {"model": GraphQAResponse},
            404: {"model": GraphQAResponse},
            409: {"model": GraphQAResponse},
            429: {"model": GraphQAResponse},
            503: {"model": GraphQAResponse},
        },
    )
    def stream_question(
        payload: APIQARequest,
        request: Request,
        api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    ):
        request_id = str(uuid.uuid4())
        session_id: Optional[str] = None
        session_lock_token: Optional[str] = None
        history: list[ConversationTurn] = []
        active_settings: APISettings = request.app.state.settings
        authenticator: APIKeyAuthenticator = request.app.state.authenticator
        store: LimitStore = request.app.state.limit_store
        session_store: ConversationStore = request.app.state.session_store
        engine: QAEngine = request.app.state.engine

        try:
            client_id = authenticator.authenticate(api_key)
        except AuthenticationError:
            return _error_response(
                request_id=request_id,
                question="",
                answer="请求未获授权。",
                code=ErrorCode.UNAUTHORIZED,
                message="API Key 缺失或无效",
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )

        session_id = str(payload.session_id or uuid.uuid4())
        try:
            rate_decision = store.consume_rate_limit(client_id, request_id)
        except Exception as exc:
            logger.warning("流式请求限流检查失败: %s", type(exc).__name__)
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求限流服务不可用，请稍后重试。",
                code=ErrorCode.RATE_LIMIT_UNAVAILABLE,
                message="请求限流服务暂时不可用",
                status_code=503,
                session_id=session_id,
            )

        rate_headers = rate_decision.headers()
        if not rate_decision.allowed:
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求过于频繁，请稍后重试。",
                code=ErrorCode.RATE_LIMITED,
                message="请求超过当前客户端限流额度",
                status_code=429,
                headers=rate_headers,
                session_id=session_id,
            )

        try:
            concurrency_decision = store.acquire_concurrency(request_id)
        except Exception as exc:
            logger.warning("流式请求并发租约获取失败: %s", type(exc).__name__)
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="请求限流服务不可用，请稍后重试。",
                code=ErrorCode.RATE_LIMIT_UNAVAILABLE,
                message="请求限流服务暂时不可用",
                status_code=503,
                headers=rate_headers,
                session_id=session_id,
            )
        if not concurrency_decision.allowed:
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="当前问答请求较多，请稍后重试。",
                code=ErrorCode.SERVICE_BUSY,
                message="问答服务已达到全局并发上限",
                status_code=503,
                headers=rate_headers,
                session_id=session_id,
            )

        try:
            locked = session_store.acquire_lock(
                client_id,
                session_id,
                request_id,
            )
        except SessionStoreError as exc:
            logger.warning("获取流式会话锁失败: %s", str(exc))
            try:
                store.release_concurrency(request_id)
            except Exception:
                pass
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="会话服务暂时不可用，请稍后重试。",
                code=ErrorCode.SESSION_UNAVAILABLE,
                message="会话服务暂时不可用",
                status_code=503,
                headers=rate_headers,
                session_id=session_id,
            )
        if not locked:
            try:
                store.release_concurrency(request_id)
            except Exception:
                pass
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="该会话正在处理其他问题，请稍后重试。",
                code=ErrorCode.SESSION_BUSY,
                message="同一会话不能并发处理多个请求",
                status_code=409,
                headers=rate_headers,
                session_id=session_id,
            )
        session_lock_token = request_id
        if payload.session_id is None:
            try:
                session_store.save(client_id, session_id, [])
            except SessionStoreError as exc:
                logger.warning("创建流式会话失败: %s", str(exc))
                try:
                    session_store.release_lock(
                        client_id,
                        session_id,
                        request_id,
                    )
                except SessionStoreError:
                    pass
                try:
                    store.release_concurrency(request_id)
                except Exception:
                    pass
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="会话服务暂时不可用，请稍后重试。",
                    code=ErrorCode.SESSION_UNAVAILABLE,
                    message="无法创建会话",
                    status_code=503,
                    headers=rate_headers,
                    session_id=session_id,
                )
        else:
            try:
                loaded_history = session_store.load(client_id, session_id)
            except SessionStoreError as exc:
                logger.warning("读取流式会话历史失败: %s", str(exc))
                try:
                    session_store.release_lock(client_id, session_id, request_id)
                except SessionStoreError:
                    pass
                try:
                    store.release_concurrency(request_id)
                except Exception:
                    pass
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="会话服务暂时不可用，请稍后重试。",
                    code=ErrorCode.SESSION_UNAVAILABLE,
                    message="会话服务暂时不可用",
                    status_code=503,
                    headers=rate_headers,
                    session_id=session_id,
                )
            if loaded_history is None:
                try:
                    session_store.release_lock(client_id, session_id, request_id)
                except SessionStoreError:
                    pass
                try:
                    store.release_concurrency(request_id)
                except Exception:
                    pass
                return _error_response(
                    request_id=request_id,
                    question=payload.question,
                    answer="会话不存在或已过期。",
                    code=ErrorCode.SESSION_NOT_FOUND,
                    message="未找到可用的会话",
                    status_code=404,
                    headers=rate_headers,
                    session_id=session_id,
                )
            history = loaded_history

        heartbeat = ConcurrencyHeartbeat(
            store=store,
            request_id=request_id,
            interval_seconds=max(
                1.0,
                active_settings.concurrency_lease_seconds / 3,
            ),
        )
        try:
            heartbeat.start()
        except Exception as exc:
            logger.error("启动流式并发租约续租失败: %s", type(exc).__name__)
            if session_lock_token is not None:
                try:
                    session_store.release_lock(
                        client_id,
                        session_id,
                        session_lock_token,
                    )
                except SessionStoreError:
                    pass
            try:
                store.release_concurrency(request_id)
            except Exception:
                pass
            return _error_response(
                request_id=request_id,
                question=payload.question,
                answer="问答服务暂时不可用，请稍后重试。",
                code=ErrorCode.INTERNAL_ERROR,
                message="问答服务初始化请求失败",
                status_code=500,
                headers=rate_headers,
                session_id=session_id,
            )

        def event_stream() -> Iterator[str]:
            try:
                stream_method = getattr(engine, "stream_answer", None)
                if not callable(stream_method):
                    response = engine.answer(
                        payload.question,
                        request_id=request_id,
                        conversation_history=history,
                        session_id=session_id,
                    )
                    yield _format_sse(
                        GraphQAStreamEvent(
                            event="stage",
                            data={
                                "stage": "answer_from_graph",
                                "label": "生成回答",
                            },
                        )
                    )
                    if response.answer:
                        yield _format_sse(
                            GraphQAStreamEvent(
                                event="answer_delta",
                                data={"delta": response.answer},
                            )
                        )
                    if response.error is not None:
                        yield _format_sse(
                            GraphQAStreamEvent(
                                event="error",
                                data=response.error.model_dump(mode="json"),
                            )
                        )
                    else:
                        turn = ConversationTurn(
                            question=payload.question,
                            answer=response.answer,
                            linked_entities=response.linked_entities,
                            grounded=response.grounded,
                        )
                        try:
                            session_store.save(
                                client_id,
                                session_id,
                                [*history, turn],
                            )
                        except SessionStoreError as exc:
                            logger.warning("保存流式会话历史失败: %s", str(exc))
                            response = GraphQAResponse(
                                request_id=request_id,
                                question=payload.question,
                                answer="会话服务暂时不可用，请稍后重试。",
                                grounded=False,
                                error=ErrorInfo(
                                    code=ErrorCode.SESSION_UNAVAILABLE,
                                    message="本轮回答未能保存到会话",
                                ),
                                session_id=session_id,
                            )
                            yield _format_sse(
                                GraphQAStreamEvent(
                                    event="error",
                                    data=response.error.model_dump(mode="json"),
                                )
                            )
                    yield _format_sse(
                        GraphQAStreamEvent(
                            event="done",
                            data={"response": response.model_dump(mode="json")},
                        )
                    )
                    return

                for event in stream_method(
                    payload.question,
                    request_id=request_id,
                    conversation_history=history,
                    session_id=session_id,
                ):
                    if event.event != "done":
                        yield _format_sse(event)
                        continue
                    response = GraphQAResponse.model_validate(
                        event.data["response"]
                    )
                    if response.error is None:
                        turn = ConversationTurn(
                            question=payload.question,
                            answer=response.answer,
                            linked_entities=response.linked_entities,
                            grounded=response.grounded,
                        )
                        try:
                            session_store.save(
                                client_id,
                                session_id,
                                [*history, turn],
                            )
                        except SessionStoreError as exc:
                            logger.warning("保存流式会话历史失败: %s", str(exc))
                            response = GraphQAResponse(
                                request_id=request_id,
                                question=payload.question,
                                answer="会话服务暂时不可用，请稍后重试。",
                                grounded=False,
                                error=ErrorInfo(
                                    code=ErrorCode.SESSION_UNAVAILABLE,
                                    message="本轮回答未能保存到会话",
                                ),
                                session_id=session_id,
                            )
                            response = response.model_copy(
                                update={"session_id": session_id}
                            )
                            yield _format_sse(
                                GraphQAStreamEvent(
                                    event="error",
                                    data=response.error.model_dump(mode="json"),
                                )
                            )
                    yield _format_sse(
                        GraphQAStreamEvent(
                            event="done",
                            data={"response": response.model_dump(mode="json")},
                        )
                    )
            except Exception as exc:
                logger.error("流式问答处理失败: %s", type(exc).__name__)
                response = GraphQAResponse(
                    request_id=request_id,
                    question=payload.question,
                    answer="系统处理问题失败，请稍后重试。",
                    grounded=False,
                    error=ErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR,
                        message="问答流式处理发生内部错误",
                    ),
                    session_id=session_id,
                )
                yield _format_sse(
                    GraphQAStreamEvent(
                        event="error",
                        data=response.error.model_dump(mode="json"),
                    )
                )
                yield _format_sse(
                    GraphQAStreamEvent(
                        event="done",
                        data={"response": response.model_dump(mode="json")},
                    )
                )
            finally:
                if session_lock_token is not None:
                    try:
                        session_store.release_lock(
                            client_id,
                            session_id,
                            session_lock_token,
                        )
                    except SessionStoreError as exc:
                        logger.warning("释放流式会话锁失败: %s", str(exc))
                heartbeat.stop()
                try:
                    store.release_concurrency(request_id)
                except Exception as exc:
                    logger.warning(
                        "释放流式并发租约失败: %s",
                        type(exc).__name__,
                    )

        stream_headers = {
            **rate_headers,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    @application.delete(
        "/api/v1/sessions/{session_id}",
        status_code=204,
        responses={
            401: {"model": GraphQAResponse},
            503: {"model": GraphQAResponse},
        },
    )
    def delete_session(
        session_id: UUID,
        request: Request,
        api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    ):
        request_id = str(uuid.uuid4())
        authenticator: APIKeyAuthenticator = request.app.state.authenticator
        session_store: ConversationStore = request.app.state.session_store
        try:
            client_id = authenticator.authenticate(api_key)
        except AuthenticationError:
            return _error_response(
                request_id=request_id,
                question="",
                answer="请求未获授权。",
                code=ErrorCode.UNAUTHORIZED,
                message="API Key 缺失或无效",
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )
        try:
            lock_token = str(uuid.uuid4())
            locked = session_store.acquire_lock(
                client_id,
                str(session_id),
                lock_token,
            )
        except SessionStoreError as exc:
            logger.warning("获取删除会话锁失败: %s", str(exc))
            return _error_response(
                request_id=request_id,
                question="",
                answer="会话服务暂时不可用，请稍后重试。",
                code=ErrorCode.SESSION_UNAVAILABLE,
                message="会话服务暂时不可用",
                status_code=503,
                session_id=str(session_id),
            )
        if not locked:
            return _error_response(
                request_id=request_id,
                question="",
                answer="该会话正在处理其他问题，请稍后重试。",
                code=ErrorCode.SESSION_BUSY,
                message="同一会话不能并发删除",
                status_code=409,
                session_id=str(session_id),
            )
        try:
            session_store.delete(client_id, str(session_id))
        except SessionStoreError as exc:
            logger.warning("删除会话失败: %s", str(exc))
            return _error_response(
                request_id=request_id,
                question="",
                answer="会话服务暂时不可用，请稍后重试。",
                code=ErrorCode.SESSION_UNAVAILABLE,
                message="会话服务暂时不可用",
                status_code=503,
                session_id=str(session_id),
            )
        finally:
            try:
                session_store.release_lock(
                    client_id,
                    str(session_id),
                    lock_token,
                )
            except SessionStoreError as exc:
                logger.warning("释放删除会话锁失败: %s", str(exc))
        return Response(status_code=204)

    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
