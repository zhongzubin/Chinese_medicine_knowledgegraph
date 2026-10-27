# -*- coding: utf-8 -*-
"""受控的 Neo4j 只读仓储。"""

import time
from typing import Any, Dict, List, Mapping, Optional

from neo4j import READ_ACCESS
from neo4j.graph import Node, Relationship

from graph_qa.cypher_guard import CypherGuard, ValidatedCypher
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import CypherPlan, ErrorCode, QueryResult
from graph_qa.schema import GraphSchema, SUPPORTED_ENTITY_LABELS

ENTITY_INDEX_QUERY = """
MATCH (node)
UNWIND labels(node) AS label
WITH node, label
WHERE label IN $labels AND node.name IS NOT NULL
RETURN label, node.name AS name, node.alias AS alias
ORDER BY label, name
""".strip()


class RepositoryError(RuntimeError):
    """表示仓储层拒绝或无法完成只读查询。"""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Node):
        return {
            "labels": sorted(value.labels),
            "properties": {key: _json_safe(item) for key, item in dict(value).items()},
        }
    if isinstance(value, Relationship):
        return {
            "type": value.type,
            "properties": {key: _json_safe(item) for key, item in dict(value).items()},
        }
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return str(value)


class Neo4jRepository:
    def __init__(
        self,
        driver: Any,
        schema: GraphSchema,
        guard: CypherGuard,
        database: Optional[str] = None,
        query_timeout_seconds: float = 8.0,
        max_results: int = 50,
    ):
        if query_timeout_seconds <= 0:
            raise ValueError("query_timeout_seconds 必须大于 0")
        if max_results < 1:
            raise ValueError("max_results 必须大于等于 1")
        self.driver = driver
        self.schema = schema
        self.guard = guard
        self.database = database
        self.query_timeout_seconds = query_timeout_seconds
        self.max_results = max_results

    def close(self) -> None:
        self.driver.close()

    def _session_options(self) -> Dict[str, Any]:
        options: Dict[str, Any] = {"default_access_mode": READ_ACCESS}
        if self.database:
            options["database"] = self.database
        return options

    def load_entity_index(self) -> EntityIndex:
        """读取六类实体的名称和别名，形成进程内只读索引。"""
        try:
            with self.driver.session(**self._session_options()) as session:
                result = session.run(
                    ENTITY_INDEX_QUERY,
                    labels=list(SUPPORTED_ENTITY_LABELS),
                )
                rows = result.data()
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.QUERY_EXECUTION_FAILED,
                "读取图谱实体索引失败，请检查 Neo4j 连接和只读权限",
            ) from exc

        records = []
        for row in rows:
            label = row.get("label")
            name = row.get("name")
            if label not in self.schema.labels or not isinstance(name, str) or not name.strip():
                continue
            alias = row.get("alias")
            records.append(
                EntityRecord(
                    label=label,
                    name=name.strip(),
                    alias=alias if isinstance(alias, str) else None,
                )
            )
        return EntityIndex.from_records(records)

    def _run_transaction(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]],
        *,
        fetch_limit: Optional[int],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        transaction = None
        try:
            with self.driver.session(**self._session_options()) as session:
                transaction = session.begin_transaction(timeout=self.query_timeout_seconds)
                result = transaction.run(query, parameters or {})
                if fetch_limit is None:
                    records = result.data()
                else:
                    records = result.fetch(fetch_limit)
                for record in records:
                    if isinstance(record, Mapping):
                        row = dict(record)
                    else:
                        row = record.data()
                    rows.append({str(key): _json_safe(value) for key, value in row.items()})
                transaction.commit()
                transaction = None
                return rows
        except Exception:
            if transaction is not None:
                try:
                    transaction.rollback()
                except Exception:
                    pass
            raise

    def explain(self, plan: CypherPlan) -> ValidatedCypher:
        """在只读事务中规划查询，检测语法和 Schema 错误。"""
        validated = self.guard.validate(plan)
        try:
            self._run_transaction(
                f"EXPLAIN {validated.plan.cypher}",
                validated.plan.parameters,
                fetch_limit=1,
            )
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.CYPHER_INVALID,
                "Cypher 语法或规划校验失败",
            ) from exc
        return validated

    def execute_read(self, plan: CypherPlan) -> QueryResult:
        """执行经过二次校验的只读查询。"""
        validated = self.guard.validate(plan)
        started_at = time.perf_counter()
        try:
            rows = self._run_transaction(
                validated.plan.cypher,
                validated.plan.parameters,
                fetch_limit=validated.limit,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = "图谱查询超时" if code == ErrorCode.QUERY_TIMEOUT else "图谱查询执行失败"
            raise RepositoryError(code, message) from exc

        return QueryResult(
            cypher=validated.plan.cypher,
            parameters=validated.plan.parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=validated.limit,
        )
