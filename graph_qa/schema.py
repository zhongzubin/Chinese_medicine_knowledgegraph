# -*- coding: utf-8 -*-
"""读取并约束当前 Neo4j 图谱元数据。"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_METADATA_PATH = PROJECT_ROOT / "metadata" / "graph_metadata.json"

SUPPORTED_ENTITY_LABELS: Tuple[str, ...] = (
    "Herb",
    "Formula",
    "Disease",
    "Symptom",
    "Effect",
    "Source",
)
SUPPORTED_RELATION_TYPES: FrozenSet[str] = frozenset(
    {
        "TREATS_DISEASE",
        "ALLEVIATES_SYMPTOM",
        "HAS_EFFECT",
        "HAS_INGREDIENT",
        "HAS_SYMPTOM",
        "FROM_SOURCE",
    }
)
EVIDENCE_RETURN_ALIASES: FrozenSet[str] = frozenset(
    {
        "subject",
        "subject_label",
        "relationship",
        "object",
        "object_label",
        "source",
    }
)


class SchemaError(ValueError):
    """表示元数据缺失、格式错误或超出 M1 支持范围。"""


def _require_list(metadata: Mapping[str, Any], key: str) -> list:
    value = metadata.get(key)
    if not isinstance(value, list):
        raise SchemaError(f"元数据字段 {key} 必须是数组")
    return value


@dataclass(frozen=True)
class GraphSchema:
    format_version: str
    labels: Mapping[str, FrozenSet[str]]
    relationship_types: FrozenSet[str]
    triple_patterns: FrozenSet[Tuple[str, str, str]]

    @property
    def entity_labels(self) -> FrozenSet[str]:
        return frozenset(self.labels)

    @property
    def property_keys(self) -> FrozenSet[str]:
        return frozenset(
            property_name
            for property_names in self.labels.values()
            for property_name in property_names
        )

    def assert_label(self, label: str) -> None:
        if label not in self.labels:
            raise SchemaError(f"未知实体标签: {label}")

    def assert_relationship(self, relationship_type: str) -> None:
        if relationship_type not in self.relationship_types:
            raise SchemaError(f"未知关系类型: {relationship_type}")

    def assert_pattern(self, subject_label: str, relationship_type: str, object_label: str) -> None:
        key = (subject_label, relationship_type, object_label)
        if key not in self.triple_patterns:
            raise SchemaError(
                "未定义的三元组模式: "
                f"({subject_label})-[:{relationship_type}]->({object_label})"
            )

    def prompt_context(self) -> str:
        """生成精简的 Schema 上下文，不包含属性样例值。"""
        label_lines = []
        for label in SUPPORTED_ENTITY_LABELS:
            properties = sorted(self.labels.get(label, frozenset()))
            label_lines.append(f"- {label}: {', '.join(properties) or '无属性'}")

        relationship_lines = [
            f"- {relationship_type}" for relationship_type in sorted(self.relationship_types)
        ]
        pattern_lines = [
            f"- ({subject})-[{relationship}]->({object})"
            for subject, relationship, object in sorted(self.triple_patterns)
        ]
        return "\n".join(
            [
                "实体标签及属性：",
                *label_lines,
                "",
                "关系类型：",
                *relationship_lines,
                "",
                "允许的三元组模式：",
                *pattern_lines,
                "",
                "证据字段固定为 subject, subject_label, relationship, object, object_label, source。",
            ]
        )

    @classmethod
    def from_file(cls, path: Path = DEFAULT_METADATA_PATH) -> "GraphSchema":
        if not path.exists():
            raise SchemaError(f"未找到图元数据: {path}")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaError(f"图元数据不是有效 JSON: {exc}") from exc
        return cls.from_mapping(metadata)

    @classmethod
    def from_mapping(cls, metadata: Mapping[str, Any]) -> "GraphSchema":
        format_version = metadata.get("format_version")
        if not isinstance(format_version, str) or not format_version:
            raise SchemaError("元数据缺少 format_version")

        labels: Dict[str, Set[str]] = {}
        for item in _require_list(metadata, "node_labels"):
            if not isinstance(item, dict):
                raise SchemaError("node_labels 中的项目必须是对象")
            label = item.get("label")
            if label not in SUPPORTED_ENTITY_LABELS:
                continue
            property_names = set()
            for property_item in item.get("properties") or []:
                if not isinstance(property_item, dict):
                    raise SchemaError(f"{label} 的属性项必须是对象")
                property_name = property_item.get("name")
                if isinstance(property_name, str) and property_name:
                    property_names.add(property_name)
            if "name" not in property_names:
                raise SchemaError(f"{label} 缺少 M1 必需的 name 属性")
            labels[label] = property_names

        missing_labels = set(SUPPORTED_ENTITY_LABELS) - set(labels)
        if missing_labels:
            raise SchemaError(f"元数据缺少 M1 实体标签: {', '.join(sorted(missing_labels))}")

        relationship_types = set()
        for item in _require_list(metadata, "relationship_types"):
            if not isinstance(item, dict):
                raise SchemaError("relationship_types 中的项目必须是对象")
            relationship_type = item.get("type")
            if relationship_type in SUPPORTED_RELATION_TYPES:
                relationship_types.add(relationship_type)

        patterns = set()
        for item in _require_list(metadata, "triple_patterns"):
            if not isinstance(item, dict):
                raise SchemaError("triple_patterns 中的项目必须是对象")
            subject = item.get("subject_label")
            relationship = item.get("relationship_type")
            object_label = item.get("object_label")
            if (
                subject in labels
                and relationship in relationship_types
                and object_label in labels
            ):
                patterns.add((subject, relationship, object_label))

        if not patterns:
            raise SchemaError("元数据没有 M1 可用的三元组模式")

        return cls(
            format_version=format_version,
            labels={label: frozenset(properties) for label, properties in labels.items()},
            relationship_types=frozenset(relationship_types),
            triple_patterns=frozenset(patterns),
        )
