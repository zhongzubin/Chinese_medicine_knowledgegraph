# -*- coding: utf-8 -*-
"""受控的 Neo4j 只读仓储。"""

import time
from typing import Any, Dict, List, Mapping, Optional

from neo4j import READ_ACCESS
from neo4j.graph import Node, Relationship

from graph_qa.cypher_guard import CypherGuard, ValidatedCypher
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import (
    AggregateQueryPlan,
    AttributeQueryPlan,
    CypherPlan,
    ErrorCode,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    NeighborQueryPlan,
    QueryResult,
)
from graph_qa.schema import GraphSchema, SUPPORTED_ENTITY_LABELS

ENTITY_INDEX_QUERY = """
MATCH (node)
UNWIND labels(node) AS label
WITH node, label
WHERE label IN $labels AND node.name IS NOT NULL
RETURN label, node.name AS name, node.alias AS alias
ORDER BY label, name
""".strip()
ATTRIBUTE_QUERY_TEMPLATE = """
MATCH (subject)
WHERE any(label IN labels(subject) WHERE label IN $labels)
  AND subject.name IN $entity_names
OPTIONAL MATCH (subject)-[:FROM_SOURCE]->(source_node:Source)
WITH subject, collect(DISTINCT source_node.name) AS source_names
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       properties(subject) AS properties,
       [name IN source_names WHERE name IS NOT NULL] AS sources
ORDER BY subject_label, subject
LIMIT {limit}
""".strip()
AGGREGATE_QUERY_TEMPLATE = """
MATCH (subject:{subject_label})-[relationship:{relationship}]->(object:{object_label})
WHERE {anchor}.name IN $entity_names
RETURN count(DISTINCT {result}) AS count
LIMIT 1
""".strip()


def neighbor_query(limit: int, include_source: bool) -> str:
    source_match = (
        "\nOPTIONAL MATCH (subject)-[:FROM_SOURCE]->(source_node:Source)"
        "\nWHERE labels(subject)[0] IN $source_labels"
        if include_source
        else ""
    )
    with_clause = (
        "WITH subject, relationship, object, "
        "collect(DISTINCT source_node.name) AS source_names"
        if include_source
        else "WITH subject, relationship, object"
    )
    source_return = (
        "[name IN source_names WHERE name IS NOT NULL] AS sources"
        if include_source
        else "[] AS sources"
    )
    return f"""
MATCH (subject)-[relationship]->(object)
WHERE (
        (
            ($direction = 'outgoing' OR $direction = 'both')
            AND labels(subject)[0] = $entity_label
            AND subject.name = $entity_name
        )
        OR (
            ($direction = 'incoming' OR $direction = 'both')
            AND labels(object)[0] = $entity_label
            AND object.name = $entity_name
        )
    )
AND any(pattern IN $patterns WHERE
        labels(subject)[0] = pattern.subject_label
        AND type(relationship) = pattern.relationship
        AND labels(object)[0] = pattern.object_label
    )
{source_match}
{with_clause}
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       type(relationship) AS relationship,
       object.name AS object,
       labels(object)[0] AS object_label,
       {source_return}
ORDER BY subject_label, subject, relationship, object_label, object
LIMIT {limit}
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

    def execute_neighbors(
        self,
        plan: NeighborQueryPlan,
    ) -> QueryResult:
        """执行受控一跳邻居查询，不接受调用方提供的 Cypher。"""
        try:
            self.schema.assert_label(plan.entity.label)
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "邻居查询包含未知实体标签",
            ) from exc
        requested_relationships = set(plan.relationship_types)
        invalid_relationships = (
            requested_relationships - self.schema.relationship_types
        )
        if invalid_relationships:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "邻居查询包含未允许的关系类型",
            )

        entity_key = (plan.entity.label, plan.entity.name)
        patterns = []
        for subject_label, relationship, object_label in sorted(
            self.schema.triple_patterns
        ):
            if (
                requested_relationships
                and relationship not in requested_relationships
            ):
                continue
            is_outgoing = subject_label == plan.entity.label
            is_incoming = object_label == plan.entity.label
            if plan.direction == "outgoing" and not is_outgoing:
                continue
            if plan.direction == "incoming" and not is_incoming:
                continue
            if not (is_outgoing or is_incoming):
                continue
            patterns.append(
                {
                    "subject_label": subject_label,
                    "relationship": relationship,
                    "object_label": object_label,
                }
            )

        limit = min(plan.limit, self.max_results)
        source_labels = sorted(
            subject_label
            for subject_label, relationship, object_label
            in self.schema.triple_patterns
            if relationship == "FROM_SOURCE" and object_label == "Source"
        )
        include_source = bool(source_labels)
        query = neighbor_query(limit, include_source)
        parameters = {
            "direction": plan.direction,
            "entity_label": plan.entity.label,
            "entity_name": plan.entity.name,
            "patterns": patterns,
        }
        if include_source:
            parameters["source_labels"] = source_labels
        started_at = time.perf_counter()
        try:
            raw_rows = self._run_transaction(
                query,
                parameters,
                fetch_limit=limit,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = (
                "图谱查询超时"
                if code == ErrorCode.QUERY_TIMEOUT
                else "图谱查询执行失败"
            )
            raise RepositoryError(code, message) from exc

        pattern_keys = {
            (
                pattern["subject_label"],
                pattern["relationship"],
                pattern["object_label"],
            )
            for pattern in patterns
        }
        rows_by_key: Dict[
            tuple[str, str, str, str, str],
            Dict[str, Any],
        ] = {}
        for row in raw_rows:
            subject = row.get("subject")
            subject_label = row.get("subject_label")
            relationship = row.get("relationship")
            object_name = row.get("object")
            object_label = row.get("object_label")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    subject,
                    subject_label,
                    relationship,
                    object_name,
                    object_label,
                )
            ):
                continue
            if (
                subject_label not in self.schema.labels
                or object_label not in self.schema.labels
                or relationship not in self.schema.relationship_types
                or (
                    subject_label,
                    relationship,
                    object_label,
                )
                not in pattern_keys
            ):
                continue
            is_outgoing = (
                (subject_label, subject) == entity_key
            )
            is_incoming = (
                (object_label, object_name) == entity_key
            )
            if plan.direction == "outgoing" and not is_outgoing:
                continue
            if plan.direction == "incoming" and not is_incoming:
                continue
            if not (is_outgoing or is_incoming):
                continue

            sources = row.get("sources")
            source_names = [
                source.strip()
                for source in (
                    sources if isinstance(sources, list) else []
                )
                if isinstance(source, str) and source.strip()
            ]
            key = (
                subject.strip(),
                subject_label,
                relationship,
                object_name.strip(),
                object_label,
            )
            existing = rows_by_key.get(key)
            if existing is None:
                existing = {
                    "subject": subject.strip(),
                    "subject_label": subject_label,
                    "relationship": relationship,
                    "object": object_name.strip(),
                    "object_label": object_label,
                    "sources": [],
                }
                rows_by_key[key] = existing
            existing_sources = existing["sources"]
            for source_name in source_names:
                if source_name not in existing_sources:
                    existing_sources.append(source_name)

        rows = [
            {
                **row,
                "source": "；".join(row["sources"]) or None,
            }
            for row in rows_by_key.values()
        ]
        for row in rows:
            row.pop("sources", None)
        return QueryResult(
            cypher=query,
            parameters=parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=limit,
        )

    def execute_attributes(
        self,
        plan: AttributeQueryPlan,
    ) -> QueryResult:
        """执行受控节点属性查询，不接受任意 Cypher。"""
        selected_attributes: Dict[str, set[str]] = {}
        for selection in plan.selections:
            allowed = set(self.schema.queryable_attributes(selection.label))
            requested = set(selection.attributes)
            invalid = requested - allowed
            if invalid:
                raise RepositoryError(
                    ErrorCode.CYPHER_REJECTED,
                    "属性查询包含未允许的字段",
                )
            selected_attributes.setdefault(selection.label, set()).update(
                requested
            )

        used_keys = {(item.label, item.name) for item in plan.used_entities}
        if not used_keys:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "属性查询缺少已链接实体",
            )
        labels = sorted({label for label, _name in used_keys})
        if any(label not in selected_attributes for label in labels):
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "属性查询缺少实体标签选择",
            )

        parameters = {
            "labels": labels,
            "entity_names": sorted({name for _label, name in used_keys}),
        }
        query = ATTRIBUTE_QUERY_TEMPLATE.format(limit=self.max_results)
        started_at = time.perf_counter()
        try:
            raw_rows = self._run_transaction(
                query,
                parameters,
                fetch_limit=self.max_results,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = (
                "图谱查询超时"
                if code == ErrorCode.QUERY_TIMEOUT
                else "图谱查询执行失败"
            )
            raise RepositoryError(code, message) from exc

        rows: List[Dict[str, Any]] = []
        for row in raw_rows:
            label = row.get("subject_label")
            name = row.get("subject")
            if (label, name) not in used_keys or label not in selected_attributes:
                continue
            properties = row.get("properties")
            if not isinstance(properties, Mapping):
                continue
            filtered = {
                key: _json_safe(properties.get(key))
                for key in sorted(selected_attributes[label])
                if properties.get(key) not in (None, "", [], {})
            }
            rows.append(
                {
                    "subject": name,
                    "subject_label": label,
                    "properties": filtered,
                    "sources": row.get("sources") or [],
                }
            )

        return QueryResult(
            cypher=query,
            parameters=parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=self.max_results,
        )

    def execute_aggregate(
        self,
        plan: AggregateQueryPlan,
    ) -> QueryResult:
        """执行受控的一跳关系计数。"""
        try:
            self.schema.assert_pattern(
                plan.subject_label,
                plan.relationship,
                plan.object_label,
            )
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "聚合查询不符合图谱 Schema",
            ) from exc

        anchor_label = (
            plan.subject_label
            if plan.direction == "outgoing"
            else plan.object_label
        )
        if any(item.label != anchor_label for item in plan.used_entities):
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "聚合查询实体与关系方向不匹配",
            )
        if any(
            item.label not in {plan.subject_label, plan.object_label}
            for item in plan.used_entities
        ):
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "聚合查询包含未使用的实体标签",
            )

        entity_names = sorted(
            {item.name for item in plan.used_entities}
        )
        if plan.require_all_entities:
            if plan.direction != "incoming":
                raise RepositoryError(
                    ErrorCode.CYPHER_REJECTED,
                    "多条件聚合只支持反向实体交集",
                )
            if len(entity_names) < 2:
                raise RepositoryError(
                    ErrorCode.CYPHER_REJECTED,
                    "多条件聚合至少需要两个实体",
                )
            exists_clauses = []
            parameters = {}
            for index, entity_name in enumerate(entity_names):
                parameter_name = f"entity_{index}"
                parameters[parameter_name] = entity_name
                exists_clauses.append(
                    "EXISTS { "
                    f"MATCH (subject)-[:{plan.relationship}]->"
                    f"(required:{plan.object_label}) "
                    f"WHERE required.name = ${parameter_name} "
                    "}"
                )
            query = (
                f"MATCH (subject:{plan.subject_label})\n"
                f"WHERE {' AND '.join(exists_clauses)}\n"
                "RETURN count(DISTINCT subject) AS count\n"
                "LIMIT 1"
            )
        else:
            anchor = "subject" if plan.direction == "outgoing" else "object"
            result = "object" if plan.direction == "outgoing" else "subject"
            query = AGGREGATE_QUERY_TEMPLATE.format(
                subject_label=plan.subject_label,
                relationship=plan.relationship,
                object_label=plan.object_label,
                anchor=anchor,
                result=result,
            )
            parameters = {"entity_names": entity_names}
        started_at = time.perf_counter()
        try:
            rows = self._run_transaction(
                query,
                parameters,
                fetch_limit=1,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = (
                "图谱查询超时"
                if code == ErrorCode.QUERY_TIMEOUT
                else "图谱查询执行失败"
            )
            raise RepositoryError(code, message) from exc

        return QueryResult(
            cypher=query,
            parameters=parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=1,
        )

    def execute_multi_condition_list(
        self,
        plan: MultiConditionQueryPlan,
    ) -> QueryResult:
        """返回同时满足多个一跳条件的结果及对应关系证据。"""
        try:
            self.schema.assert_pattern(
                plan.subject_label,
                plan.relationship,
                plan.object_label,
            )
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多条件查询不符合图谱 Schema",
            ) from exc
        if any(
            entity.label != plan.object_label
            for entity in plan.used_entities
        ):
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多条件查询实体标签不匹配",
            )
        entity_names = sorted(
            {entity.name for entity in plan.used_entities}
        )
        if len(entity_names) < 2:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多条件查询至少需要两个实体",
            )

        exists_clauses = []
        parameters = {}
        for index, entity_name in enumerate(entity_names):
            parameter_name = f"entity_{index}"
            parameters[parameter_name] = entity_name
            exists_clauses.append(
                "EXISTS { "
                f"MATCH (subject)-[:{plan.relationship}]->"
                f"(required:{plan.object_label}) "
                f"WHERE required.name = ${parameter_name} "
                "}"
            )
        query = f"""
MATCH (subject:{plan.subject_label})
WHERE {' AND '.join(exists_clauses)}
WITH DISTINCT subject
MATCH (subject)-[relationship:{plan.relationship}]->(object:{plan.object_label})
WHERE object.name IN $entity_names
OPTIONAL MATCH (subject)-[:FROM_SOURCE]->(source_node:Source)
WITH subject, relationship, object,
     collect(DISTINCT source_node.name) AS source_names
WITH subject,
     collect(DISTINCT {{
         relationship: type(relationship),
         object: object.name,
         object_label: labels(object)[0],
         sources: [name IN source_names WHERE name IS NOT NULL]
     }}) AS conditions
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       conditions
ORDER BY subject
LIMIT {self.max_results}
""".strip()
        parameters["entity_names"] = entity_names
        started_at = time.perf_counter()
        try:
            rows = self._run_transaction(
                query,
                parameters,
                fetch_limit=self.max_results,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = (
                "图谱查询超时"
                if code == ErrorCode.QUERY_TIMEOUT
                else "图谱查询执行失败"
            )
            raise RepositoryError(code, message) from exc
        return QueryResult(
            cypher=query,
            parameters=parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=self.max_results,
        )

    def execute_multi_hop(
        self,
        plan: MultiHopQueryPlan,
    ) -> QueryResult:
        """执行受控的 2～3 跳查询并返回完整路径记录。"""
        if not 2 <= len(plan.steps) <= 3:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多跳查询必须包含 2 到 3 个步骤",
            )
        first = plan.steps[0]
        for previous, current in zip(plan.steps, plan.steps[1:]):
            if previous.object_label != current.subject_label:
                raise RepositoryError(
                    ErrorCode.CYPHER_REJECTED,
                    "多跳查询节点链不连续",
                )
        try:
            for step in plan.steps:
                self.schema.assert_pattern(
                    step.subject_label,
                    step.relationship,
                    step.object_label,
                )
        except Exception as exc:
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多跳查询不符合图谱 Schema",
            ) from exc
        if any(
            entity.label != first.subject_label
            for entity in plan.used_entities
        ):
            raise RepositoryError(
                ErrorCode.CYPHER_REJECTED,
                "多跳查询起始实体标签不匹配",
            )

        can_load_source = (
            first.subject_label,
            "FROM_SOURCE",
            "Source",
        ) in self.schema.triple_patterns
        source_match = (
            "\nOPTIONAL MATCH "
            "(root)-[:FROM_SOURCE]->(source_node:Source)"
            if can_load_source
            else ""
        )
        relationship_variables = [
            "first",
            "second",
            "third",
        ][: len(plan.steps)]
        intermediate_variables = [
            f"middle_{index}"
            for index in range(1, len(plan.steps))
        ]
        node_variables = ["root", *intermediate_variables, "target"]
        path_parts = [f"({node_variables[0]}:{first.subject_label})"]
        for index, step in enumerate(plan.steps):
            path_parts.append(
                f"-[{relationship_variables[index]}:{step.relationship}]->"
                f"({node_variables[index + 1]}:{step.object_label})"
            )
        path_pattern = "".join(path_parts)
        relationships_return = ", ".join(
            f"type({variable})"
            for variable in relationship_variables
        )
        nodes_return = ", ".join(
            f"{variable}.name"
            for variable in node_variables[1:]
        )
        node_labels_return = ", ".join(
            f"labels({variable})[0]"
            for variable in node_variables[1:]
        )
        with_variables = ", ".join(
            [*node_variables, *relationship_variables]
        )
        source_return = (
            "[name IN source_names WHERE name IS NOT NULL]"
            if can_load_source
            else "[]"
        )
        with_clause = (
            f"WITH {with_variables}, "
            "collect(DISTINCT source_node.name) AS source_names"
            if can_load_source
            else f"WITH {with_variables}"
        )
        query = f"""
MATCH {path_pattern}
WHERE root.name IN $entity_names
{source_match}
{with_clause}
RETURN DISTINCT root.name AS root,
       labels(root)[0] AS root_label,
       [{relationships_return}] AS relationship_types,
       [{nodes_return}] AS node_names,
       [{node_labels_return}] AS node_labels,
       target.name AS target,
       labels(target)[0] AS target_label,
       {source_return} AS sources
ORDER BY root, target
LIMIT {self.max_results}
""".strip()
        parameters = {
            "entity_names": sorted(
                {entity.name for entity in plan.used_entities}
            )
        }
        started_at = time.perf_counter()
        try:
            rows = self._run_transaction(
                query,
                parameters,
                fetch_limit=self.max_results,
            )
        except Exception as exc:
            text = str(exc).lower()
            code = (
                ErrorCode.QUERY_TIMEOUT
                if "timeout" in text or "timed out" in text
                else ErrorCode.QUERY_EXECUTION_FAILED
            )
            message = (
                "图谱查询超时"
                if code == ErrorCode.QUERY_TIMEOUT
                else "图谱查询执行失败"
            )
            raise RepositoryError(code, message) from exc
        return QueryResult(
            cypher=query,
            parameters=parameters,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            limit_applied=self.max_results,
        )
