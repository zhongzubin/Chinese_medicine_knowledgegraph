# -*- coding: utf-8 -*-
"""导出 Neo4j 图数据库元数据，并生成适合大模型使用的结构说明。

用法:
    uv run python -m graph_database.export_metadata
    uv run python -m graph_database.export_metadata --sample-size 5
    uv run python -m graph_database.export_metadata --database neo4j

输出:
    metadata/graph_metadata.json
    metadata/graph_metadata.md

导出内容:
    1. 实体标签、节点数量和属性结构；
    2. 关系类型、关系数量和关系属性；
    3. 实际存在的 (起点标签)-[关系类型]->(终点标签) 三元组模式；
    4. 少量实体、属性值和关系实例，供大模型理解数据用。

脚本只执行只读查询，不修改图数据库，也不会把连接地址、用户名或密码写入输出。
"""

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable

from common.config import Config

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "metadata"
DEFAULT_SAMPLE_SIZE = 3
SAMPLE_VALUE_LIMIT = 80
METADATA_FORMAT_VERSION = "1.1"

DATABASE_DESCRIPTION = (
    "中医知识图谱数据库，用于描述中药、方剂、疾病、症状、功效和文献出处之间的知识关联。"
)

ENTITY_DESCRIPTIONS = {
    "Disease": "疾病实体，表示可以独立命名或识别的病名、病证或病理状态。",
    "Effect": "功效实体，表示中药或方剂所具备的治疗作用、药理作用或作用趋向。",
    "Formula": "方剂实体，表示由一味或多味药材按一定配伍和用法组成的处方。",
    "Herb": "药材实体，表示用于中医临床或中药制剂的植物、动物、矿物等药物来源。",
    "Source": "文献出处实体，表示方剂、药材或相关知识所来源的书籍、典籍或资料。",
    "Symptom": "症状实体，表示患者的主观感受或临床观察到的体征。",
}

PROPERTY_DESCRIPTIONS = {
    "alias": "实体的别名、异名或俗称，文本中可能同时包含多个名称。",
    "dosage": "用量说明，记录单次或每日剂量、用法频次以及必要的剂量限制。",
    "effect": "功效文本，描述方剂或药材的治疗作用和功能。",
    "from_source": "以文本属性记录的知识来源；与 FROM_SOURCE 关系并存，查询出处时应同时考虑两者。",
    "indication": "主治或适应症文本，记录适合使用该方剂或药材的疾病、证候或症状。",
    "ingredients": "方剂组成文本，记录处方中的药材、剂量或配伍信息。",
    "meridian": "归经信息，描述药材或方剂作用所属的脏腑和经络。",
    "name": "实体的规范名称，用作同一标签内查询具体实体的标识属性。",
    "origin": "来源或基原信息，记录药材的动植物或矿物基原、药用部位等。",
    "place": "产地或分布信息，记录药材的产地、生境和地理分布。",
    "processing": "炮制或采收加工信息，记录采集、清洗、炮炙、干燥等过程。",
    "property_flavor": "性味信息，描述药材或方剂的四气五味、毒性等属性。",
    "taboo": "禁忌与注意事项，说明不宜使用的人群、证候或配伍风险。",
    "traits": "性状特征，记录药材外观、质地、气味或毒性等鉴别描述。",
    "usage": "用法说明，描述煎服、冲服、外敷等具体使用方式。",
}

RELATION_DESCRIPTIONS = {
    "ALLEVIATES_SYMPTOM": (
        "起点实体能够缓解终点症状。绝大多数来自方剂或药材；数据库中也有少量终点为 "
        "Disease 的模式，查询时必须结合实际三元组方向。"
    ),
    "FROM_SOURCE": (
        "知识来源关系，通常表示方剂或药材来源于某部文献。数据库中还存在 "
        "Source 指向 Herb 的反向存储模式。"
    ),
    "HAS_EFFECT": "起点实体具有终点所述的中医功效。",
    "HAS_INGREDIENT": (
        "组成关系，通常表示方剂包含药材；数据库中也有少量方剂包含方剂、药材包含药材的模式。"
    ),
    "HAS_SYMPTOM": "疾病包含或表现出某个症状。",
    "TREATS_DISEASE": "起点方剂或药材用于治疗终点疾病。",
}

TRIPLE_PATTERN_DESCRIPTIONS = {
    ("Disease", "HAS_SYMPTOM", "Symptom"): "疾病表现为或包含某个症状。",
    ("Formula", "ALLEVIATES_SYMPTOM", "Symptom"): "方剂能够缓解某个症状。",
    ("Formula", "FROM_SOURCE", "Source"): "方剂来源于某部文献或典籍。",
    ("Formula", "HAS_EFFECT", "Effect"): "方剂具有某项治疗功效。",
    ("Formula", "HAS_INGREDIENT", "Formula"): (
        "一首方剂包含另一首方剂作为组成部分；这是少量存在的嵌套方剂关系。"
    ),
    ("Formula", "HAS_INGREDIENT", "Herb"): "方剂的组成中包含某种药材。",
    ("Formula", "TREATS_DISEASE", "Disease"): "方剂用于治疗某种疾病。",
    ("Herb", "ALLEVIATES_SYMPTOM", "Disease"): (
        "药材缓解以疾病标签存储的适用证或病症；该模式数量较少，查询时应严格匹配终点标签。"
    ),
    ("Herb", "ALLEVIATES_SYMPTOM", "Symptom"): "药材能够缓解某种症状。",
    ("Herb", "FROM_SOURCE", "Source"): "药材知识来源于某部文献或典籍。",
    ("Herb", "HAS_EFFECT", "Effect"): "药材具有某项治疗功效。",
    ("Herb", "HAS_INGREDIENT", "Herb"): (
        "一种药材的资料中记录了另一种药材作为组成；这是少量存在的特殊药材关系，不等同于方剂组成。"
    ),
    ("Herb", "TREATS_DISEASE", "Disease"): "药材用于治疗某种疾病。",
    ("Source", "FROM_SOURCE", "Herb"): (
        "文献出处反向关联其所记载的药材，是 FROM_SOURCE 的反向存储模式。"
    ),
}


class MetadataExportError(ValueError):
    """表示数据库元数据不符合导出条件。"""


def quote_identifier(value: str) -> str:
    """引用 Neo4j 标签或关系类型，避免动态拼接产生语法或注入问题。"""
    if not isinstance(value, str) or not value.strip():
        raise MetadataExportError("Neo4j 标签和关系类型必须是非空字符串")
    return f"`{value.replace('`', '``')}`"


def _property_description(property_name: str) -> str:
    return PROPERTY_DESCRIPTIONS.get(
        property_name,
        f"{property_name} 属性的结构性说明；具体业务含义需结合所属实体判断。",
    )


def _triple_pattern_description(
    subject_label: str,
    relationship_type: str,
    object_label: str,
) -> str:
    return TRIPLE_PATTERN_DESCRIPTIONS.get(
        (subject_label, relationship_type, object_label),
        (
            f"{subject_label} 实体通过 {relationship_type} 关系指向 "
            f"{object_label} 实体；该方向来自当前数据库的实际数据。"
        ),
    )


def enrich_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """为数据库、实体、属性、关系和三重模式补充中文语义描述。"""
    enriched = deepcopy(metadata)
    enriched["format_version"] = METADATA_FORMAT_VERSION

    database = enriched.setdefault("database", {})
    database["description"] = DATABASE_DESCRIPTION

    for node_label in enriched.get("node_labels", []):
        label = node_label.get("label", "")
        node_label["description"] = ENTITY_DESCRIPTIONS.get(
            label,
            f"{label} 类型实体的结构性说明。",
        )
        for property_metadata in node_label.get("properties", []):
            property_name = property_metadata.get("name", "")
            property_metadata["description"] = _property_description(property_name)

    for relationship in enriched.get("relationship_types", []):
        relationship_type = relationship.get("type", "")
        relationship["description"] = RELATION_DESCRIPTIONS.get(
            relationship_type,
            f"{relationship_type} 关系的结构性说明。",
        )
        for property_metadata in relationship.get("properties", []):
            property_name = property_metadata.get("name", "")
            property_metadata["description"] = _property_description(property_name)

    for pattern in enriched.get("triple_patterns", []):
        pattern["description"] = _triple_pattern_description(
            pattern.get("subject_label", ""),
            pattern.get("relationship_type", ""),
            pattern.get("object_label", ""),
        )

    return enriched


def _fetch(session: Any, query: str, **parameters: Any) -> List[Dict[str, Any]]:
    return session.run(query, **parameters).data()


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "map"
    return type(value).__name__


def _sample_value(value: Any) -> Any:
    """把样例值转成可序列化形式，并限制长度。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= SAMPLE_VALUE_LIMIT else f"{value[:SAMPLE_VALUE_LIMIT]}..."
    if isinstance(value, (list, tuple)):
        return [_sample_value(item) for item in value[:3]]
    if isinstance(value, dict):
        return {str(key): _sample_value(item) for key, item in list(value.items())[:5]}
    return _sample_value(str(value))


def _property_metadata(
    session: Any,
    match_clause: str,
    owner_expression: str,
    sample_size: int,
) -> List[Dict[str, Any]]:
    """读取匹配项的属性名称、出现次数、类型和样例。"""
    property_rows = _fetch(
        session,
        (
            f"MATCH {match_clause} "
            f"UNWIND keys({owner_expression}) AS property "
            "RETURN property, count(*) AS count "
            "ORDER BY property"
        ),
    )

    properties = []
    for row in property_rows:
        property_name = row["property"]
        samples = []
        if sample_size > 0:
            sample_rows = _fetch(
                session,
                (
                    f"MATCH {match_clause} "
                    f"WHERE {owner_expression}[$property] IS NOT NULL "
                    f"RETURN {owner_expression}[$property] AS value "
                    "LIMIT $sample_size"
                ),
                property=property_name,
                sample_size=sample_size,
            )
            samples = [
                _sample_value(item["value"])
                for item in sample_rows
                if item.get("value") is not None
            ]

        inferred_types = sorted({_infer_type(item) for item in samples})
        properties.append(
            {
                "name": property_name,
                "present_in_count": row["count"],
                "inferred_types": inferred_types,
                "sample_values": samples,
            }
        )

    if not properties:
        # 没有属性时不需要额外查询，避免产生不必要的数据库访问
        return properties
    return properties


def _collect_triple_patterns(
    session: Any,
    sample_size: int,
) -> List[Dict[str, Any]]:
    pattern_rows = _fetch(
        session,
        (
            "MATCH (source)-[relationship]->(target) "
            "UNWIND labels(source) AS source_label "
            "UNWIND labels(target) AS target_label "
            "RETURN source_label, type(relationship) AS relationship_type, "
            "target_label, count(*) AS count "
            "ORDER BY source_label, relationship_type, target_label"
        ),
    )

    patterns = []
    for row in pattern_rows:
        source_label = row["source_label"]
        relationship_type = row["relationship_type"]
        target_label = row["target_label"]
        examples = []
        if sample_size > 0:
            sample_query = (
                f"MATCH (source:{quote_identifier(source_label)})"
                f"-[relationship:{quote_identifier(relationship_type)}]->"
                f"(target:{quote_identifier(target_label)}) "
                "RETURN coalesce(source.name, elementId(source)) AS subject, "
                "coalesce(target.name, elementId(target)) AS object "
                "ORDER BY subject, object "
                "LIMIT $sample_size"
            )
            sample_rows = _fetch(session, sample_query, sample_size=sample_size)
            examples = [
                {
                    "subject": _sample_value(item["subject"]),
                    "object": _sample_value(item["object"]),
                }
                for item in sample_rows
            ]

        patterns.append(
            {
                "subject_label": source_label,
                "relationship_type": relationship_type,
                "object_label": target_label,
                "count": row["count"],
                "examples": examples,
            }
        )
    return patterns


def collect_metadata(
    session: Any,
    database: Optional[str] = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> Dict[str, Any]:
    """从已连接的 Neo4j session 收集完整图结构元数据。"""
    if sample_size < 0:
        raise ValueError("sample_size 必须大于等于 0")

    node_count = _fetch(session, "MATCH (node) RETURN count(node) AS count")[0]["count"]
    relationship_count = _fetch(
        session,
        "MATCH ()-[relationship]->() RETURN count(relationship) AS count",
    )[0]["count"]

    label_rows = _fetch(
        session,
        (
            "MATCH (node) "
            "UNWIND labels(node) AS label "
            "RETURN label, count(*) AS count "
            "ORDER BY label"
        ),
    )
    node_labels = []
    for row in label_rows:
        label = row["label"]
        properties = _property_metadata(
            session,
            f"(node:{quote_identifier(label)})",
            "node",
            sample_size,
        )
        identifier_property = (
            "name" if any(item["name"] == "name" for item in properties) else None
        )
        node_labels.append(
            {
                "label": label,
                "count": row["count"],
                "identifier_property": identifier_property,
                "properties": properties,
            }
        )

    relationship_rows = _fetch(
        session,
        (
            "MATCH ()-[relationship]->() "
            "RETURN type(relationship) AS relationship_type, count(*) AS count "
            "ORDER BY relationship_type"
        ),
    )
    relationship_types = []
    for row in relationship_rows:
        relationship_type = row["relationship_type"]
        properties = _property_metadata(
            session,
            f"()-[relationship:{quote_identifier(relationship_type)}]->()",
            "relationship",
            sample_size,
        )
        relationship_types.append(
            {
                "type": relationship_type,
                "count": row["count"],
                "properties": properties,
            }
        )

    triple_patterns = _collect_triple_patterns(session, sample_size)
    metadata = {
        "format_version": METADATA_FORMAT_VERSION,
        "database": {
            "name": database or "default",
            "node_count": node_count,
            "relationship_count": relationship_count,
        },
        "node_labels": node_labels,
        "relationship_types": relationship_types,
        "triple_patterns": triple_patterns,
        "query_guidance": {
            "entity_lookup_property": "name",
            "rules": [
                "只能使用 node_labels 中列出的实体标签",
                "只能使用 relationship_types 中列出的关系类型",
                "关系方向必须严格遵循 triple_patterns",
                "查询具体实体时优先使用 name 属性，并将用户输入作为参数传入",
                "不要假设两个标签之间存在未在 triple_patterns 中列出的关系",
            ],
        },
    }
    return enrich_metadata(metadata)


def _markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _format_samples(values: Sequence[Any]) -> str:
    if not values:
        return "-"
    return "、".join(_markdown_cell(value) for value in values)


def build_markdown(metadata: Dict[str, Any]) -> str:
    """生成适合直接加入大模型提示词的 Markdown 图结构说明。"""
    metadata = enrich_metadata(metadata)
    database = metadata["database"]
    lines = [
        "# 中医知识图谱数据库元数据",
        "",
        "## 数据库概览",
        "",
        f"- 数据库：{_markdown_cell(database['name'])}",
        f"- 节点总数：{database['node_count']}",
        f"- 关系总数：{database['relationship_count']}",
        f"- 数据库说明：{_markdown_cell(database.get('description', ''))}",
        "",
        "实体和关系以具体实例存储在图中。下面列出的三元组模式是编写 Cypher 时必须遵循的类型结构。",
        "",
        "## 实体标签与属性",
    ]

    for node_label in metadata["node_labels"]:
        lines.extend(
            [
                "",
                f"### {node_label['label']}（{node_label['count']} 个节点）",
                "",
            ]
        )
        identifier = node_label.get("identifier_property")
        if identifier:
            lines.append(f"- 实体标识属性：`{identifier}`")
        lines.append(f"- 实体说明：{_markdown_cell(node_label.get('description', ''))}")

        properties = node_label.get("properties") or []
        if not properties:
            lines.append("- 无属性")
            continue

        lines.extend(
            [
                "",
                "| 属性 | 描述 | 出现次数 | 推断类型 | 样例 |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for item in properties:
            types = "、".join(item.get("inferred_types") or []) or "-"
            lines.append(
                f"| `{_markdown_cell(item['name'])}` | "
                f"{_markdown_cell(item.get('description', ''))} | {item['present_in_count']} | "
                f"{_markdown_cell(types)} | {_format_samples(item.get('sample_values') or [])} |"
            )

    lines.extend(
        [
            "",
            "## 关系类型",
            "",
            "| 关系类型 | 描述 | 数量 | 属性 |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for relationship in metadata["relationship_types"]:
        property_names = [item["name"] for item in relationship.get("properties") or []]
        properties = "、".join(f"`{name}`" for name in property_names) or "无"
        lines.append(
            f"| `{_markdown_cell(relationship['type'])}` | "
            f"{_markdown_cell(relationship.get('description', ''))} | "
            f"{relationship['count']} | {properties} |"
        )

    lines.extend(
        [
            "",
            "## 三元组模式",
            "",
            "方向统一表示为 `(起点标签)-[:关系类型]->(终点标签)`。",
            "",
            "| 起点实体 | 关系 | 终点实体 | 模式描述 | 实例数 | 数据实例 |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for pattern in metadata["triple_patterns"]:
        examples = "；".join(
            f"{_markdown_cell(item['subject'])} -> {_markdown_cell(item['object'])}"
            for item in pattern.get("examples") or []
        )
        lines.append(
            f"| `{_markdown_cell(pattern['subject_label'])}` | "
            f"`{_markdown_cell(pattern['relationship_type'])}` | "
            f"`{_markdown_cell(pattern['object_label'])}` | "
            f"{_markdown_cell(pattern.get('description', ''))} | "
            f"{pattern['count']} | {examples or '-'} |"
        )

    guidance = metadata["query_guidance"]
    lines.extend(
        [
            "",
            "## Cypher 生成约束",
            "",
            f"- 优先使用标识属性 `{guidance['entity_lookup_property']}` 查询具体实体。",
            "- 将用户提供的名称、编号等作为 Cypher 参数，不要直接拼接为查询字符串。",
        ]
    )
    lines.extend(f"- {rule}" for rule in guidance["rules"])
    lines.append("")
    return "\n".join(lines)


def write_metadata_files(
    metadata: Dict[str, Any],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    """把元数据同时写为 JSON 和大模型提示词 Markdown。"""
    metadata = enrich_metadata(metadata)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "graph_metadata.json"
    markdown_path = output_dir / "graph_metadata.md"
    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(build_markdown(metadata), encoding="utf-8")
    return json_path, markdown_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="只读导出 Neo4j 图数据库元数据")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="元数据输出目录",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="每个属性或三元组模式保留的样例数量",
    )
    parser.add_argument("--database", help="Neo4j 数据库名称，默认使用服务端默认数据库")
    args = parser.parse_args()
    if args.sample_size < 0:
        parser.error("--sample-size 必须大于等于 0")
    return args


def main() -> None:
    args = parse_args()
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
        try:
            driver.verify_connectivity()
        except ServiceUnavailable as exc:
            raise RuntimeError(
                "无法连接 Neo4j，请确认数据库已经启动且 NEO4J_URI 可从当前机器访问"
            ) from exc
        session_options = {"database": args.database} if args.database else {}
        with driver.session(**session_options) as session:
            metadata = collect_metadata(
                session,
                database=args.database,
                sample_size=args.sample_size,
            )
    finally:
        driver.close()

    json_path, markdown_path = write_metadata_files(metadata, args.output_dir)
    print(
        f"元数据导出完成：{metadata['database']['node_count']} 个节点，"
        f"{metadata['database']['relationship_count']} 条关系，"
        f"{len(metadata['node_labels'])} 个实体标签，"
        f"{len(metadata['triple_patterns'])} 种三元组模式"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")


if __name__ == "__main__":
    main()
