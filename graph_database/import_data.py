# -*- coding: utf-8 -*-
"""将 crawler/data 下抽取出的中药、方剂实体和关系导入 Neo4j。

数据来源:
    crawler/data/extract_herb_data.json
    crawler/data/extract_formula_data.json

用法:
    uv run python -m graph_database.import_data
    uv run python -m graph_database.import_data --dry-run
    uv run python -m graph_database.import_data --batch-size 1000

导入策略:
    1. 实体使用 type 作为 Neo4j 标签，使用 name 作为唯一业务键；
    2. 同名但类型不同的实体分别保存；
    3. 重复实体的属性以首个非空值为准，后续记录只补充缺失属性；
    4. 关系完全按照 subject_type、relation、object_type 写入；
    5. 关系引用了但实体列表中未出现的节点会自动补全，避免悬空关系；
    6. 全部写入使用 MERGE，可重复执行，默认不删除数据库中的已有数据。
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from neo4j import GraphDatabase

from common.config import Config

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "crawler" / "data"
DEFAULT_DATA_FILES = (
    DEFAULT_DATA_DIR / "extract_herb_data.json",
    DEFAULT_DATA_DIR / "extract_formula_data.json",
)

ENTITY_TYPES = frozenset({"Symptom", "Disease", "Formula", "Herb", "Effect", "Source"})
RELATION_TYPES = frozenset(
    {
        "TREATS_DISEASE",
        "ALLEVIATES_SYMPTOM",
        "HAS_EFFECT",
        "HAS_INGREDIENT",
        "HAS_SYMPTOM",
        "FROM_SOURCE",
    }
)
DEFAULT_BATCH_SIZE = 500

EntityKey = Tuple[str, str]
RelationKey = Tuple[str, str, str, str, str]
RelationGroupKey = Tuple[str, str, str]


class GraphDataError(ValueError):
    """表示抽取文件不符合预期结构。"""


@dataclass(frozen=True)
class EntityRecord:
    entity_type: str
    name: str
    attributes: Dict[str, Any]


@dataclass(frozen=True)
class RelationRecord:
    subject: str
    subject_type: str
    relation: str
    object: str
    object_type: str


@dataclass(frozen=True)
class GraphData:
    entities: List[EntityRecord]
    relations: List[RelationRecord]
    record_count: int
    entity_occurrence_count: int
    relation_occurrence_count: int
    placeholder_entity_count: int


def _require_text(value: Any, field: str, location: str) -> str:
    """读取必填文本字段，并给出可以定位到源数据的错误信息。"""
    if not isinstance(value, str) or not value.strip():
        raise GraphDataError(f"{location}.{field} 必须是非空字符串")
    return value.strip()


def _parse_attributes(value: Any, location: str) -> Dict[str, Any]:
    """校验实体属性，跳过空值并返回可写入 Neo4j 的扁平属性。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise GraphDataError(f"{location}.attributes 必须是对象")

    attributes: Dict[str, Any] = {}
    for field, attribute_value in value.items():
        if attribute_value is None:
            continue
        if isinstance(attribute_value, str) and not attribute_value.strip():
            continue
        if not isinstance(attribute_value, (str, int, float, bool)):
            raise GraphDataError(
                f"{location}.attributes.{field} 暂不支持 {type(attribute_value).__name__} 类型"
            )
        attributes[str(field)] = attribute_value
    return attributes


def _parse_entity(value: Any, location: str) -> Tuple[EntityRecord, Dict[str, Any]]:
    if not isinstance(value, dict):
        raise GraphDataError(f"{location} 必须是对象")

    entity_type = _require_text(value.get("type"), "type", location)
    if entity_type not in ENTITY_TYPES:
        raise GraphDataError(f"{location}.type 不支持的实体类型: {entity_type}")
    name = _require_text(value.get("name"), "name", location)
    attributes = _parse_attributes(value.get("attributes"), location)
    return EntityRecord(entity_type, name, attributes), attributes


def _parse_relation(value: Any, location: str) -> RelationRecord:
    if not isinstance(value, dict):
        raise GraphDataError(f"{location} 必须是对象")

    subject_type = _require_text(value.get("subject_type"), "subject_type", location)
    object_type = _require_text(value.get("object_type"), "object_type", location)
    if subject_type not in ENTITY_TYPES:
        raise GraphDataError(f"{location}.subject_type 不支持的实体类型: {subject_type}")
    if object_type not in ENTITY_TYPES:
        raise GraphDataError(f"{location}.object_type 不支持的实体类型: {object_type}")

    relation = _require_text(value.get("relation"), "relation", location)
    if relation not in RELATION_TYPES:
        raise GraphDataError(f"{location}.relation 不支持的关系类型: {relation}")

    return RelationRecord(
        subject=_require_text(value.get("subject"), "subject", location),
        subject_type=subject_type,
        relation=relation,
        object=_require_text(value.get("object"), "object", location),
        object_type=object_type,
    )


def load_graph_data(paths: Sequence[Path]) -> GraphData:
    """读取一个或多个抽取文件，合并重复实体并去重关系。"""
    if not paths:
        raise GraphDataError("至少需要提供一个抽取文件")

    attributes_by_entity: Dict[EntityKey, Dict[str, Any]] = {}
    relations_by_key: Dict[RelationKey, RelationRecord] = {}
    record_count = 0
    entity_occurrence_count = 0
    relation_occurrence_count = 0

    for path in paths:
        if not path.exists():
            raise GraphDataError(f"未找到抽取文件: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GraphDataError(f"{path} 不是有效的 JSON: {exc}") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise GraphDataError(f"{path} 的顶层结构必须包含 results 数组")

        for result_index, result in enumerate(payload["results"]):
            result_location = f"{path.name}.results[{result_index}]"
            if not isinstance(result, dict):
                raise GraphDataError(f"{result_location} 必须是对象")
            extracted = result.get("extract_dict")
            if not isinstance(extracted, dict):
                raise GraphDataError(f"{result_location}.extract_dict 必须是对象")

            entities = extracted.get("entities", [])
            relations = extracted.get("relations", [])
            if not isinstance(entities, list):
                raise GraphDataError(f"{result_location}.extract_dict.entities 必须是数组")
            if not isinstance(relations, list):
                raise GraphDataError(f"{result_location}.extract_dict.relations 必须是数组")

            record_count += 1
            for entity_index, raw_entity in enumerate(entities):
                location = f"{result_location}.extract_dict.entities[{entity_index}]"
                entity, attributes = _parse_entity(raw_entity, location)
                entity_occurrence_count += 1
                existing_attributes = attributes_by_entity.setdefault(
                    (entity.entity_type, entity.name), {}
                )
                # 与抽取阶段的合并策略保持一致：先出现的非空值优先，后续补充缺失字段
                for field, attribute_value in attributes.items():
                    existing_attributes.setdefault(field, attribute_value)

            for relation_index, raw_relation in enumerate(relations):
                location = f"{result_location}.extract_dict.relations[{relation_index}]"
                relation = _parse_relation(raw_relation, location)
                relation_occurrence_count += 1
                relation_key = (
                    relation.subject_type,
                    relation.subject,
                    relation.relation,
                    relation.object_type,
                    relation.object,
                )
                relations_by_key.setdefault(relation_key, relation)

    declared_entity_count = len(attributes_by_entity)
    relations = sorted(
        relations_by_key.values(),
        key=lambda item: (item.subject_type, item.subject, item.relation, item.object_type, item.object),
    )
    for relation in relations:
        attributes_by_entity.setdefault((relation.subject_type, relation.subject), {})
        attributes_by_entity.setdefault((relation.object_type, relation.object), {})

    entities = [
        EntityRecord(entity_type, name, attributes)
        for (entity_type, name), attributes in sorted(attributes_by_entity.items())
    ]
    return GraphData(
        entities=entities,
        relations=relations,
        record_count=record_count,
        entity_occurrence_count=entity_occurrence_count,
        relation_occurrence_count=relation_occurrence_count,
        placeholder_entity_count=len(entities) - declared_entity_count,
    )


def _iter_batches(items: Sequence[Any], batch_size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def _group_relations(
    relations: Sequence[RelationRecord],
) -> Dict[RelationGroupKey, List[RelationRecord]]:
    """按两端类型和关系类型分组，以便生成固定标签的 Cypher 查询。"""
    groups: Dict[RelationGroupKey, List[RelationRecord]] = {}
    for relation in relations:
        key = (relation.subject_type, relation.relation, relation.object_type)
        groups.setdefault(key, []).append(relation)
    return groups


def _create_entity_constraints(session: Any) -> None:
    """为每种实体建立标签内唯一的 name 约束。"""
    for entity_type in sorted(ENTITY_TYPES):
        constraint_name = f"entity_{entity_type.lower()}_name_unique"
        query = (
            f"CREATE CONSTRAINT `{constraint_name}` IF NOT EXISTS "
            f"FOR (node:`{entity_type}`) REQUIRE node.name IS UNIQUE"
        )
        session.run(query).consume()


def _write_entities(tx: Any, entity_type: str, rows: Sequence[Dict[str, Any]]) -> None:
    query = (
        f"UNWIND $rows AS row "
        f"MERGE (node:`{entity_type}` {{name: row.name}}) "
        "SET node += row.attributes"
    )
    tx.run(query, rows=rows).consume()


def _write_relations(
    tx: Any,
    group: RelationGroupKey,
    rows: Sequence[Dict[str, str]],
) -> None:
    subject_type, relation_type, object_type = group
    query = (
        f"UNWIND $rows AS row "
        f"MATCH (subject:`{subject_type}` {{name: row.subject}}) "
        f"MATCH (object:`{object_type}` {{name: row.object}}) "
        f"MERGE (subject)-[:`{relation_type}`]->(object)"
    )
    tx.run(query, rows=rows).consume()


def import_graph(
    graph_data: GraphData,
    driver: Any,
    database: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """建立约束后分批写入实体和关系；所有写操作均使用 MERGE。"""
    if batch_size < 1:
        raise ValueError("batch_size 必须大于等于 1")

    session_options = {"database": database} if database else {}
    with driver.session(**session_options) as session:
        _create_entity_constraints(session)

        entities_by_type: Dict[str, List[EntityRecord]] = {}
        for entity in graph_data.entities:
            entities_by_type.setdefault(entity.entity_type, []).append(entity)

        for entity_type in sorted(entities_by_type):
            node_rows = [
                {"name": entity.name, "attributes": entity.attributes}
                for entity in entities_by_type[entity_type]
            ]
            for batch in _iter_batches(node_rows, batch_size):
                session.execute_write(_write_entities, entity_type, batch)

        for group, relations in sorted(_group_relations(graph_data.relations).items()):
            relation_rows = [
                {"subject": relation.subject, "object": relation.object}
                for relation in relations
            ]
            for batch in _iter_batches(relation_rows, batch_size):
                session.execute_write(_write_relations, group, batch)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将中药、方剂实体和关系导入 Neo4j")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="包含两个抽取 JSON 文件的目录",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="每批写入的实体或关系数量",
    )
    parser.add_argument("--database", help="Neo4j 数据库名称，默认使用服务端默认数据库")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析和统计 JSON，不连接 Neo4j",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size 必须大于等于 1")
    return args


def main() -> None:
    args = parse_args()
    paths = [
        args.data_dir / "extract_herb_data.json",
        args.data_dir / "extract_formula_data.json",
    ]
    graph_data = load_graph_data(paths)

    print(
        f"读取 {graph_data.record_count} 条记录，"
        f"合并后实体 {len(graph_data.entities)} 个"
        f"（自动补全关系端点 {graph_data.placeholder_entity_count} 个），"
        f"关系 {len(graph_data.relations)} 条"
    )
    if args.dry_run:
        print("仅解析数据，未连接 Neo4j")
        return

    config = Config()
    connection_settings = {
        "NEO4J_URI": config.NEO4J_URI,
        "NEO4J_USER": config.NEO4J_USER,
        "NEO4J_PASSWORD": config.NEO4J_PASSWORD,
    }
    missing = [name for name, value in connection_settings.items() if not value]
    if missing:
        raise RuntimeError(f"缺少 Neo4j 连接配置: {', '.join(missing)}")

    driver = GraphDatabase.driver(
        config.NEO4J_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
    )
    try:
        driver.verify_connectivity()
        import_graph(
            graph_data,
            driver,
            database=args.database,
            batch_size=args.batch_size,
        )
    finally:
        driver.close()
    print("导入完成")


if __name__ == "__main__":
    main()
