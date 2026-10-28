# -*- coding: utf-8 -*-
"""中医知识图谱问答 FastAPI 服务。"""

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any, Callable, Literal, Optional, Protocol

from fastapi import FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from graph_qa.api_security import (
    APIConfigurationError,
    APIKeyAuthenticator,
    APISettings,
    AuthenticationError,
)
from graph_qa.cli import build_engine_from_environment
from graph_qa.models import (
    ErrorCode,
    ErrorInfo,
    GraphQARequest,
    GraphQAResponse,
    StrictModel,
)
from graph_qa.redis_limits import (
    ConcurrencyHeartbeat,
    LimitStore,
    RedisLimitStore,
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
    ) -> GraphQAResponse:
        ...


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    schema_version: Literal["1.0"] = "1.0"


EngineFactory = Callable[[APISettings], QAEngine]
LimitStoreFactory = Callable[[APISettings], LimitStore]


def _default_engine_factory(settings: APISettings) -> QAEngine:
    return build_engine_from_environment(
        database=settings.database,
        max_results=settings.max_results,
    )


def _default_limit_store_factory(settings: APISettings) -> LimitStore:
    return RedisLimitStore(settings)


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
) -> JSONResponse:
    response = GraphQAResponse(
        request_id=request_id,
        question=question,
        answer=answer,
        grounded=False,
        error=ErrorInfo(code=code, message=message),
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


def create_app(
    settings: Optional[APISettings] = None,
    engine_factory: Optional[EngineFactory] = None,
    limit_store_factory: Optional[LimitStoreFactory] = None,
) -> FastAPI:
    """创建应用；依赖只在 lifespan 中初始化。"""

    engine_builder = engine_factory or _default_engine_factory
    store_builder = limit_store_factory or _default_limit_store_factory

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        active_settings: Optional[APISettings] = None
        active_store: Optional[LimitStore] = None
        active_engine: Optional[QAEngine] = None
        try:
            active_settings = settings or APISettings.from_environment()
            authenticator = APIKeyAuthenticator(active_settings)
            active_store = store_builder(active_settings)
            if not active_store.ping():
                raise APIConfigurationError("Redis 连接不可用")
            active_engine = engine_builder(active_settings)
        except APIConfigurationError:
            _close_quietly(active_store)
            _close_quietly(active_engine)
            raise
        except Exception as exc:
            _close_quietly(active_store)
            _close_quietly(active_engine)
            raise APIConfigurationError("问答 API 初始化失败") from None

        application.state.settings = active_settings
        application.state.authenticator = authenticator
        application.state.limit_store = active_store
        application.state.engine = active_engine
        try:
            yield
        finally:
            repository = getattr(active_engine, "repository", None)
            _close_quietly(repository)
            _close_quietly(active_store)

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
            healthy = bool(request.app.state.limit_store.ping())
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
            429: {"model": GraphQAResponse},
            500: {"model": GraphQAResponse},
            502: {"model": GraphQAResponse},
            503: {"model": GraphQAResponse},
            504: {"model": GraphQAResponse},
        },
    )
    def answer_question(
        payload: GraphQARequest,
        request: Request,
        api_key: Annotated[Optional[str], Security(api_key_header)] = None,
    ):
        request_id = str(uuid.uuid4())
        active_settings: APISettings = request.app.state.settings
        authenticator: APIKeyAuthenticator = request.app.state.authenticator
        store: LimitStore = request.app.state.limit_store
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
            heartbeat.start()
            response = engine.answer(payload.question, request_id=request_id)
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
            )
        finally:
            heartbeat.stop()
            try:
                store.release_concurrency(request_id)
            except Exception as exc:
                logger.warning("Redis 并发租约释放失败: %s", type(exc).__name__)

        return JSONResponse(
            status_code=_status_for_response(response),
            content=response.model_dump(mode="json"),
            headers=rate_headers,
        )

    return application


app = create_app()
