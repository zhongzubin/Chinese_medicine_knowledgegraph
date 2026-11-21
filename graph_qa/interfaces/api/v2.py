# -*- coding: utf-8 -*-
"""V2 公共 API、匿名会话和 Tool 接口。"""

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from graph_qa.application.contracts import (
    EntityNeighborsData,
    EvidencePathsData,
    GraphEvidenceError,
    GraphEvidenceErrorCode,
    ResolveEntityData,
)
from graph_qa.application.graph_evidence_service import GraphEvidenceService
from graph_qa.application.qa_service import QAApplicationService
from graph_qa.interfaces.api.contracts import (
    ApiEnvelope,
    ApiError,
    ApiWarning,
    EntityDetailData,
    EntitySearchRequest,
    EvidencePathRequest,
    ResponseMeta,
    SchemaPatternData,
    SchemaSummaryData,
    ToolCatalogData,
    V2QARequest,
)
from graph_qa.models import (
    ConversationTurn,
    ErrorCode,
    GraphQAResponse,
    GraphQAStreamEvent,
)
from graph_qa.redis_limits import (
    ConcurrencyHeartbeat,
    LimitStore,
    RateLimitDecision,
)
from graph_qa.session_store import (
    ConversationStore,
    SessionStoreError,
)
from graph_qa.tools.contracts import (
    ToolErrorCode,
    ToolResult,
)
from graph_qa.tools.executor import ToolExecutor
from graph_qa.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

DEMO_SESSION_COOKIE = "tcmkg_demo_session"
ANONYMOUS_SESSION_CLIENT_ID = "anonymous-web"
ANONYMOUS_WARNING_CODE = "GRAPH_QA_WARNING"
TOOL_WARNING_CODE = "TOOL_WARNING"


@dataclass
class QASessionContext:
    client_id: str
    session_id: str
    history: list[ConversationTurn]
    is_new: bool


class QASessionFailure(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _anonymous_rate_client_id(request: Request) -> str:
    host = request.client.host if request.client is not None else "unknown"
    settings = request.app.state.settings
    if settings.demo_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        first_hop = forwarded.split(",", 1)[0].strip()
        if first_hop:
            host = first_hop
    digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:32]
    return f"anonymous-{digest}"


def _request_id() -> str:
    return str(uuid.uuid4())


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _set_demo_cookie(
    response,
    request: Request,
    session_id: str,
) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=DEMO_SESSION_COOKIE,
        value=session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.demo_cookie_secure,
        path="/api/v2",
    )


def _error_response(
    request_id: str,
    started_at: float,
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Optional[dict[str, str]] = None,
    tool_version: Optional[str] = None,
) -> JSONResponse:
    envelope = ApiEnvelope[Any](
        request_id=request_id,
        data=None,
        error=ApiError(code=code, message=message),
        meta=ResponseMeta(
            tool_version=tool_version,
            duration_ms=_duration_ms(started_at),
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _rate_limit_response(
    request: Request,
    request_id: str,
    started_at: float,
) -> tuple[Optional[RateLimitDecision], Optional[JSONResponse]]:
    store: LimitStore = request.app.state.limit_store
    client_id = _anonymous_rate_client_id(request)
    try:
        decision = store.consume_rate_limit(client_id, request_id)
    except Exception as exc:
        logger.warning("V2 限流检查失败: %s", type(exc).__name__)
        return None, _error_response(
            request_id,
            started_at,
            503,
            ErrorCode.RATE_LIMIT_UNAVAILABLE.value,
            "请求限流服务暂时不可用",
        )
    if not decision.allowed:
        return decision, _error_response(
            request_id,
            started_at,
            429,
            ErrorCode.RATE_LIMITED.value,
            "请求超过匿名演示限流额度",
            headers=decision.headers(),
        )
    return decision, None


def _tool_status(code: ToolErrorCode) -> int:
    return {
        ToolErrorCode.INVALID_ARGUMENT: 422,
        ToolErrorCode.TOOL_NOT_FOUND: 404,
        ToolErrorCode.ENTITY_NOT_FOUND: 404,
        ToolErrorCode.QUERY_TIMEOUT: 504,
        ToolErrorCode.QUERY_EXECUTION_FAILED: 502,
        ToolErrorCode.TOOL_TIMEOUT: 504,
        ToolErrorCode.INTERNAL_ERROR: 500,
    }.get(code, 500)


def _tool_envelope(
    result: ToolResult[Any],
    request_id: str,
    started_at: float,
    *,
    headers: Optional[dict[str, str]] = None,
    tool_version: Optional[str] = None,
) -> JSONResponse:
    status_code = 200 if result.error is None else _tool_status(result.error.code)
    envelope = ApiEnvelope[Any](
        request_id=request_id,
        run_id=result.run_id,
        data=result.data if result.error is None else None,
        evidence=result.evidence,
        warnings=[
            ApiWarning(code=TOOL_WARNING_CODE, message=message)
            for message in result.warnings
        ],
        error=(
            ApiError(
                code=result.error.code.value,
                message=result.error.message,
            )
            if result.error is not None
            else None
        ),
        meta=ResponseMeta(
            tool_version=tool_version,
            duration_ms=_duration_ms(started_at),
            degraded=result.degraded,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _qa_status(response: GraphQAResponse) -> int:
    if response.error is None:
        return 200
    return {
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.UNSAFE_REQUEST: 400,
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.SESSION_NOT_FOUND: 404,
        ErrorCode.SESSION_BUSY: 409,
        ErrorCode.RATE_LIMITED: 429,
        ErrorCode.RATE_LIMIT_UNAVAILABLE: 503,
        ErrorCode.SERVICE_BUSY: 503,
        ErrorCode.SESSION_UNAVAILABLE: 503,
        ErrorCode.CONFIG_ERROR: 503,
        ErrorCode.MODEL_UNAVAILABLE: 503,
        ErrorCode.MODEL_OUTPUT_INVALID: 502,
        ErrorCode.CYPHER_REJECTED: 400,
        ErrorCode.CYPHER_INVALID: 502,
        ErrorCode.QUERY_TIMEOUT: 504,
        ErrorCode.QUERY_EXECUTION_FAILED: 502,
        ErrorCode.NO_GRAPH_EVIDENCE: 502,
    }.get(response.error.code, 500)


def _qa_envelope(
    response: GraphQAResponse,
    request_id: str,
    started_at: float,
    *,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    degraded = bool(response.error) or any(
        "降级" in warning
        for warning in response.warnings
    )
    if (
        response.verification is not None
        and response.verification.fallback_used
    ):
        degraded = True
    envelope = ApiEnvelope[GraphQAResponse](
        request_id=request_id,
        run_id=response.request_id,
        data=response,
        evidence=response.evidence,
        warnings=[
            ApiWarning(code=ANONYMOUS_WARNING_CODE, message=message)
            for message in response.warnings
        ],
        error=(
            ApiError(
                code=response.error.code.value,
                message=response.error.message,
            )
            if response.error is not None
            else None
        ),
        meta=ResponseMeta(
            duration_ms=_duration_ms(started_at),
            degraded=degraded,
        ),
    )
    return JSONResponse(
        status_code=_qa_status(response),
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


def _format_sse(event: GraphQAStreamEvent) -> str:
    payload = json.dumps(
        event.data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.event}\ndata: {payload}\n\n"


def build_validation_error_response() -> JSONResponse:
    started_at = time.perf_counter()
    return _error_response(
        _request_id(),
        started_at,
        422,
        ErrorCode.INVALID_REQUEST.value,
        "请求参数无效",
    )


def _requested_session_id(
    request: Request,
    payload: V2QARequest,
) -> tuple[Optional[str], bool]:
    if payload.session_id is not None:
        return str(payload.session_id), False
    cookie_value = request.cookies.get(DEMO_SESSION_COOKIE)
    if cookie_value:
        try:
            return str(UUID(cookie_value)), False
        except ValueError:
            logger.info("忽略格式无效的匿名会话 Cookie")
    return None, True


def _prepare_session(
    request: Request,
    payload: V2QARequest,
    request_id: str,
) -> QASessionContext:
    session_store: ConversationStore = request.app.state.session_store
    client_id = ANONYMOUS_SESSION_CLIENT_ID
    session_id, is_new = _requested_session_id(request, payload)
    session_id = session_id or str(uuid.uuid4())
    try:
        locked = session_store.acquire_lock(
            client_id,
            session_id,
            request_id,
        )
    except SessionStoreError as exc:
        logger.warning("获取 V2 会话锁失败: %s", str(exc))
        raise QASessionFailure(
            ErrorCode.SESSION_UNAVAILABLE,
            "会话服务暂时不可用",
            503,
        ) from exc
    if not locked:
        raise QASessionFailure(
            ErrorCode.SESSION_BUSY,
            "同一会话不能并发处理多个请求",
            409,
        )

    try:
        if is_new:
            session_store.save(client_id, session_id, [])
            history: list[ConversationTurn] = []
        else:
            loaded = session_store.load(client_id, session_id)
            if loaded is None:
                raise QASessionFailure(
                    ErrorCode.SESSION_NOT_FOUND,
                    "未找到可用的会话",
                    404,
                )
            history = loaded
    except QASessionFailure:
        try:
            session_store.release_lock(
                client_id,
                session_id,
                request_id,
            )
        except SessionStoreError:
            pass
        raise
    except SessionStoreError as exc:
        logger.warning("V2 会话读写失败: %s", str(exc))
        try:
            session_store.release_lock(
                client_id,
                session_id,
                request_id,
            )
        except SessionStoreError:
            pass
        raise QASessionFailure(
            ErrorCode.SESSION_UNAVAILABLE,
            "会话服务暂时不可用",
            503,
        ) from exc
    return QASessionContext(
        client_id=client_id,
        session_id=session_id,
        history=history,
        is_new=is_new,
    )


def _release_session(
    request: Request,
    context: QASessionContext,
    request_id: str,
) -> None:
    session_store: ConversationStore = request.app.state.session_store
    try:
        session_store.release_lock(
            context.client_id,
            context.session_id,
            request_id,
        )
    except SessionStoreError as exc:
        logger.warning("释放 V2 会话锁失败: %s", str(exc))


def _save_completed_turn(
    request: Request,
    context: QASessionContext,
    question: str,
    response: GraphQAResponse,
) -> None:
    if response.error is not None:
        return
    session_store: ConversationStore = request.app.state.session_store
    turn = ConversationTurn(
        question=question,
        answer=response.answer,
        linked_entities=response.linked_entities,
        grounded=response.grounded,
    )
    session_store.save(
        context.client_id,
        context.session_id,
        [*context.history, turn],
    )


def _tool_runtime(
    request: Request,
) -> tuple[
    Optional[GraphEvidenceService],
    Optional[ToolRegistry],
    Optional[ToolExecutor],
]:
    return (
        getattr(request.app.state, "graph_evidence_service", None),
        getattr(request.app.state, "tool_registry", None),
        getattr(request.app.state, "tool_executor", None),
    )


def _tool_unavailable(
    request_id: str,
    started_at: float,
    headers: dict[str, str],
) -> JSONResponse:
    return _error_response(
        request_id,
        started_at,
        503,
        "TOOL_UNAVAILABLE",
        "图谱 Tool 尚未就绪",
        headers=headers,
    )


def create_v2_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["v2"])

    @router.post(
        "/qa",
        response_model=ApiEnvelope[GraphQAResponse],
        responses={
            400: {"model": ApiEnvelope[Any]},
            404: {"model": ApiEnvelope[Any]},
            409: {"model": ApiEnvelope[Any]},
            429: {"model": ApiEnvelope[Any]},
            503: {"model": ApiEnvelope[Any]},
            504: {"model": ApiEnvelope[Any]},
        },
    )
    def answer_question(payload: V2QARequest, request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()

        store: LimitStore = request.app.state.limit_store
        try:
            concurrency = store.acquire_concurrency(request_id)
        except Exception as exc:
            logger.warning("V2 并发租约获取失败: %s", type(exc).__name__)
            return _error_response(
                request_id,
                started_at,
                503,
                ErrorCode.RATE_LIMIT_UNAVAILABLE.value,
                "请求限流服务暂时不可用",
                headers=headers,
            )
        if not concurrency.allowed:
            return _error_response(
                request_id,
                started_at,
                503,
                ErrorCode.SERVICE_BUSY.value,
                "问答服务已达到全局并发上限",
                headers=headers,
            )

        heartbeat = ConcurrencyHeartbeat(
            store=store,
            request_id=request_id,
            interval_seconds=max(
                1.0,
                request.app.state.settings.concurrency_lease_seconds / 3,
            ),
        )
        context: Optional[QASessionContext] = None
        try:
            try:
                heartbeat.start()
            except Exception as exc:
                logger.error(
                    "启动 V2 并发租约续租失败: %s",
                    type(exc).__name__,
                )
                return _error_response(
                    request_id,
                    started_at,
                    500,
                    ErrorCode.INTERNAL_ERROR.value,
                    "问答服务初始化请求失败",
                    headers=headers,
                )
            try:
                context = _prepare_session(request, payload, request_id)
            except QASessionFailure as exc:
                response = _error_response(
                    request_id,
                    started_at,
                    exc.status_code,
                    exc.code.value,
                    exc.message,
                    headers=headers,
                )
                return response
            qa_service: QAApplicationService = request.app.state.qa_service
            try:
                response = qa_service.answer(
                    payload.question,
                    request_id=request_id,
                    session_id=context.session_id,
                    history=context.history,
                )
            except Exception as exc:
                logger.error("V2 问答处理失败: %s", type(exc).__name__)
                response = GraphQAResponse(
                    request_id=request_id,
                    question=payload.question,
                    answer="系统处理问题失败，请稍后重试。",
                    grounded=False,
                    error={
                        "code": ErrorCode.INTERNAL_ERROR,
                        "message": "问答处理发生内部错误",
                    },
                    session_id=context.session_id,
                )
            try:
                _save_completed_turn(
                    request,
                    context,
                    payload.question,
                    response,
                )
            except SessionStoreError as exc:
                logger.warning("保存 V2 会话历史失败: %s", str(exc))
                response = GraphQAResponse(
                    request_id=request_id,
                    question=payload.question,
                    answer="会话服务暂时不可用，请稍后重试。",
                    grounded=False,
                    error={
                        "code": ErrorCode.SESSION_UNAVAILABLE,
                        "message": "本轮回答未能保存到会话",
                    },
                    session_id=context.session_id,
                )
            response = response.model_copy(
                update={"session_id": None}
            )
            http_response = _qa_envelope(
                response,
                request_id,
                started_at,
                headers=headers,
            )
            _set_demo_cookie(
                http_response,
                request,
                context.session_id,
            )
            return http_response
        finally:
            if context is not None:
                _release_session(request, context, request_id)
            heartbeat.stop()
            try:
                store.release_concurrency(request_id)
            except Exception as exc:
                logger.warning(
                    "释放 V2 并发租约失败: %s",
                    type(exc).__name__,
                )

    @router.post(
        "/qa/stream",
        responses={
            200: {"content": {"text/event-stream": {}}},
            404: {"model": ApiEnvelope[Any]},
            409: {"model": ApiEnvelope[Any]},
            429: {"model": ApiEnvelope[Any]},
            503: {"model": ApiEnvelope[Any]},
        },
    )
    def stream_question(payload: V2QARequest, request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()

        store: LimitStore = request.app.state.limit_store
        try:
            concurrency = store.acquire_concurrency(request_id)
        except Exception as exc:
            logger.warning("V2 流式并发租约获取失败: %s", type(exc).__name__)
            return _error_response(
                request_id,
                started_at,
                503,
                ErrorCode.RATE_LIMIT_UNAVAILABLE.value,
                "请求限流服务暂时不可用",
                headers=headers,
            )
        if not concurrency.allowed:
            return _error_response(
                request_id,
                started_at,
                503,
                ErrorCode.SERVICE_BUSY.value,
                "问答服务已达到全局并发上限",
                headers=headers,
            )

        context: Optional[QASessionContext] = None
        heartbeat = ConcurrencyHeartbeat(
            store=store,
            request_id=request_id,
            interval_seconds=max(
                1.0,
                request.app.state.settings.concurrency_lease_seconds / 3,
            ),
        )
        try:
            heartbeat.start()
            context = _prepare_session(request, payload, request_id)
        except QASessionFailure as exc:
            heartbeat.stop()
            try:
                store.release_concurrency(request_id)
            except Exception:
                pass
            return _error_response(
                request_id,
                started_at,
                exc.status_code,
                exc.code.value,
                exc.message,
                headers=headers,
            )
        except Exception as exc:
            heartbeat.stop()
            try:
                store.release_concurrency(request_id)
            except Exception:
                pass
            logger.error("V2 流式初始化失败: %s", type(exc).__name__)
            return _error_response(
                request_id,
                started_at,
                500,
                ErrorCode.INTERNAL_ERROR.value,
                "问答服务初始化请求失败",
                headers=headers,
            )

        qa_service: QAApplicationService = request.app.state.qa_service

        def event_stream() -> Iterator[str]:
            done_emitted = False
            try:
                for event in qa_service.stream_answer(
                    payload.question,
                    request_id=request_id,
                    session_id=context.session_id,
                    history=context.history,
                ):
                    if event.event != "done":
                        yield _format_sse(event)
                        continue
                    response = GraphQAResponse.model_validate(
                        event.data["response"]
                    )
                    try:
                        _save_completed_turn(
                            request,
                            context,
                            payload.question,
                            response,
                        )
                    except SessionStoreError as exc:
                        logger.warning("保存 V2 流式会话失败: %s", str(exc))
                        response = GraphQAResponse(
                            request_id=request_id,
                            question=payload.question,
                            answer="会话服务暂时不可用，请稍后重试。",
                            grounded=False,
                            error={
                                "code": ErrorCode.SESSION_UNAVAILABLE,
                                "message": "本轮回答未能保存到会话",
                            },
                            session_id=None,
                        )
                        yield _format_sse(
                            GraphQAStreamEvent(
                                event="error",
                                data=response.error.model_dump(mode="json"),
                            )
                        )
                    response = response.model_copy(
                        update={"session_id": None}
                    )
                    yield _format_sse(
                        GraphQAStreamEvent(
                            event="done",
                            data={"response": response.model_dump(mode="json")},
                        )
                    )
                    done_emitted = True
                if not done_emitted:
                    raise RuntimeError("流式问答未返回完成事件")
            except Exception as exc:
                logger.error("V2 流式问答失败: %s", type(exc).__name__)
                response = GraphQAResponse(
                    request_id=request_id,
                    question=payload.question,
                    answer="系统处理问题失败，请稍后重试。",
                    grounded=False,
                    error={
                        "code": ErrorCode.INTERNAL_ERROR,
                        "message": "问答流式处理发生内部错误",
                    },
                    session_id=None,
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
                _release_session(request, context, request_id)
                heartbeat.stop()
                try:
                    store.release_concurrency(request_id)
                except Exception as exc:
                    logger.warning(
                        "释放 V2 流式并发租约失败: %s",
                        type(exc).__name__,
                    )

        response = StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                **headers,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        _set_demo_cookie(response, request, context.session_id)
        return response

    @router.get(
        "/tools",
        response_model=ApiEnvelope[ToolCatalogData],
    )
    def list_tools(request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        _service, registry, _executor = _tool_runtime(request)
        if registry is None:
            return _tool_unavailable(request_id, started_at, headers)
        data = ToolCatalogData(tools=registry.list_definitions())
        envelope = ApiEnvelope[ToolCatalogData](
            request_id=request_id,
            data=data,
            meta=ResponseMeta(duration_ms=_duration_ms(started_at)),
        )
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            headers=headers,
        )

    @router.get(
        "/schema/summary",
        response_model=ApiEnvelope[SchemaSummaryData],
    )
    def schema_summary(request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        service, _registry, _executor = _tool_runtime(request)
        if service is None:
            return _tool_unavailable(request_id, started_at, headers)
        schema = service.schema
        data = SchemaSummaryData(
            format_version=schema.format_version,
            entity_labels=sorted(schema.labels),
            relationship_types=sorted(schema.relationship_types),
            triple_patterns=[
                SchemaPatternData(
                    subject_label=subject,
                    relationship=relationship,
                    object_label=object_label,
                )
                for subject, relationship, object_label
                in sorted(schema.triple_patterns)
            ],
        )
        envelope = ApiEnvelope[SchemaSummaryData](
            request_id=request_id,
            data=data,
            meta=ResponseMeta(duration_ms=_duration_ms(started_at)),
        )
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            headers=headers,
        )

    @router.post(
        "/entities/search",
        response_model=ApiEnvelope[ResolveEntityData],
    )
    def search_entities(payload: EntitySearchRequest, request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        _service, registry, executor = _tool_runtime(request)
        if registry is None or executor is None:
            return _tool_unavailable(request_id, started_at, headers)
        definition = registry.get("resolve_entity").definition
        result = executor.execute(
            "resolve_entity",
            payload.model_dump(exclude_none=True),
        )
        return _tool_envelope(
            result,
            request_id,
            started_at,
            headers=headers,
            tool_version=definition.version,
        )

    @router.get(
        "/entities/{entity_id}",
        response_model=ApiEnvelope[EntityDetailData],
    )
    def get_entity(entity_id: str, request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        service, _registry, _executor = _tool_runtime(request)
        if service is None:
            return _tool_unavailable(request_id, started_at, headers)
        try:
            entity = service.require_entity(entity_id)
        except GraphEvidenceError as exc:
            return _error_response(
                request_id,
                started_at,
                404
                if exc.code == GraphEvidenceErrorCode.ENTITY_NOT_FOUND
                else 422,
                exc.code.value,
                exc.message,
                headers=headers,
            )
        data = EntityDetailData(
            entity=entity,
            available_attributes=list(
                service.schema.queryable_attributes(entity.label)
            ),
        )
        envelope = ApiEnvelope[EntityDetailData](
            request_id=request_id,
            data=data,
            meta=ResponseMeta(duration_ms=_duration_ms(started_at)),
        )
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            headers=headers,
        )

    @router.get(
        "/entities/{entity_id}/neighbors",
        response_model=ApiEnvelope[EntityNeighborsData],
    )
    def get_neighbors(
        entity_id: str,
        request: Request,
        relationship: Optional[list[str]] = Query(default=None),
        direction: str = Query(default="both"),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        _service, registry, executor = _tool_runtime(request)
        if registry is None or executor is None:
            return _tool_unavailable(request_id, started_at, headers)
        definition = registry.get("query_neighbors").definition
        result = executor.execute(
            "query_neighbors",
            {
                "entity_id": entity_id,
                "relationship_types": relationship or [],
                "direction": direction,
                "limit": limit,
            },
        )
        return _tool_envelope(
            result,
            request_id,
            started_at,
            headers=headers,
            tool_version=definition.version,
        )

    @router.post(
        "/paths",
        response_model=ApiEnvelope[EvidencePathsData],
    )
    def find_paths(payload: EvidencePathRequest, request: Request):
        started_at = time.perf_counter()
        request_id = _request_id()
        rate_decision, rate_error = _rate_limit_response(
            request,
            request_id,
            started_at,
        )
        if rate_error is not None:
            return rate_error
        headers = rate_decision.headers()
        _service, registry, executor = _tool_runtime(request)
        if registry is None or executor is None:
            return _tool_unavailable(request_id, started_at, headers)
        definition = registry.get("find_evidence_paths").definition
        result = executor.execute(
            "find_evidence_paths",
            payload.model_dump(mode="json"),
        )
        return _tool_envelope(
            result,
            request_id,
            started_at,
            headers=headers,
            tool_version=definition.version,
        )

    return router
