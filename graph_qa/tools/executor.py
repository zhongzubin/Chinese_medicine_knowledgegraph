# -*- coding: utf-8 -*-
"""Tool 参数校验、超时、结果上限和稳定错误处理。"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from graph_qa.application.contracts import GraphEvidenceError
from graph_qa.tools.contracts import (
    ToolError,
    ToolErrorCode,
    ToolErrorDetail,
    ToolExecutionContext,
    ToolResult,
)
from graph_qa.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        max_workers: int = 4,
    ):
        if max_workers < 1:
            raise ValueError("max_workers 必须大于等于 1")
        self.registry = registry
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="graph-tool",
        )

    def __enter__(self) -> "ToolExecutor":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def execute(
        self,
        tool_id: str,
        arguments: Dict[str, Any],
        version: Optional[str] = None,
    ) -> ToolResult[Any]:
        run_id = str(uuid4())
        spec = self.registry.get(tool_id, version)
        if spec is None:
            return self._error_result(
                run_id,
                ToolErrorCode.TOOL_NOT_FOUND,
                "未找到指定 Tool",
            )
        try:
            payload = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            details = [
                ToolErrorDetail(
                    location=".".join(
                        str(item) for item in error["loc"]
                    ),
                    reason=str(error["type"]),
                )
                for error in exc.errors()
            ]
            return self._error_result(
                run_id,
                ToolErrorCode.INVALID_ARGUMENT,
                "Tool 参数无效",
                details,
            )

        context = ToolExecutionContext(
            run_id=run_id,
            timeout_seconds=spec.definition.timeout_seconds,
            max_rows=spec.definition.max_rows,
        )
        future = self._pool.submit(spec.handler, payload, context)
        try:
            result = future.result(timeout=spec.definition.timeout_seconds)
        except FutureTimeout:
            future.cancel()
            return self._error_result(
                run_id,
                ToolErrorCode.TOOL_TIMEOUT,
                "Tool 执行超时",
            )
        except GraphEvidenceError as exc:
            return self._error_result(
                run_id,
                ToolErrorCode(exc.code.value),
                exc.message,
            )
        except Exception:
            return self._error_result(
                run_id,
                ToolErrorCode.INTERNAL_ERROR,
                "Tool 执行失败",
            )

        if not isinstance(result, ToolResult):
            return self._error_result(
                run_id,
                ToolErrorCode.INTERNAL_ERROR,
                "Tool 返回了无效结果",
            )
        if self._exceeds_limit(result, spec.definition.max_rows):
            return self._error_result(
                run_id,
                ToolErrorCode.INTERNAL_ERROR,
                "Tool 返回结果超过上限",
            )
        return result.model_copy(update={"run_id": run_id})

    @staticmethod
    def _exceeds_limit(
        result: ToolResult[Any],
        max_rows: int,
    ) -> bool:
        if len(result.evidence) > max_rows:
            return True
        if result.data is None:
            return False
        for attribute in type(result.data).model_fields:
            value = getattr(result.data, attribute, None)
            if isinstance(value, list) and len(value) > max_rows:
                return True
        return False

    @staticmethod
    def _error_result(
        run_id: str,
        code: ToolErrorCode,
        message: str,
        details: Optional[list[ToolErrorDetail]] = None,
    ) -> ToolResult[Any]:
        return ToolResult(
            run_id=run_id,
            error=ToolError(
                code=code,
                message=message,
                details=details or [],
            ),
        )
