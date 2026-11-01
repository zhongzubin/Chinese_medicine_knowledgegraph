# -*- coding: utf-8 -*-
"""从当前只读图谱生成 200 条产品质量固定集。"""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from dotenv import load_dotenv
from neo4j import GraphDatabase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_CASES_PATH = (
    PROJECT_ROOT / "evaluation" / "product_quality_cases.jsonl"
)
QA_CASES_PATH = PROJECT_ROOT / "evaluation" / "qa_cases.jsonl"
DEFAULT_OUTPUT_PATH = BASE_CASES_PATH
TARGET_CASE_COUNT = 200
BASE_CASE_IDS = (
    "attribute-mahuang-flavor-meridian",
    "attribute-mahuang-taboo",
    "attribute-renshen-dosage",
    "attribute-huangqi-flavor",
    "attribute-guizhi-meridian",
    "attribute-shengjiang-taboo",
    "attribute-siwutang-effect",
    "attribute-sijunzi-usage",
    "attribute-sijunzi-indication",
    "attribute-guizhitang-effect",
    "aggregate-sijunzi-ingredients",
    "aggregate-formulas-with-renshen",
    "aggregate-mahuang-diseases",
    "aggregate-mahuang-symptoms",
    "aggregate-renshen-baizhu-formulas",
    "aggregate-renshen-baizhu-fuling-formulas",
    "multi-condition-formula-list",
    "multi-hop-formula-herb-effects",
    "multi-hop-formula-herb-diseases",
)
ATTRIBUTE_SPECS = (
    ("Herb", "property_flavor", "herb_attribute", "{name}的性味是什么？"),
    ("Herb", "meridian", "herb_attribute", "{name}归经是什么？"),
    ("Herb", "dosage", "herb_attribute", "{name}用量多少？"),
    ("Herb", "taboo", "herb_attribute", "{name}有哪些禁忌？"),
    ("Herb", "effect", "herb_attribute", "{name}有什么功效？"),
    ("Herb", "indication", "herb_attribute", "{name}主治什么？"),
    ("Herb", "usage", "herb_attribute", "{name}怎么用？"),
    ("Herb", "alias", "herb_attribute", "{name}有哪些别名？"),
    ("Herb", "place", "herb_attribute", "{name}产地在哪里？"),
    ("Herb", "processing", "herb_attribute", "{name}如何炮制？"),
    ("Formula", "effect", "formula_attribute", "{name}有什么功效？"),
    ("Formula", "indication", "formula_attribute", "{name}主治什么？"),
    ("Formula", "usage", "formula_attribute", "{name}怎么用？"),
    ("Formula", "taboo", "formula_attribute", "{name}有哪些禁忌？"),
    ("Formula", "ingredients", "formula_attribute", "{name}组成是什么？"),
)

RELATION_FORWARD_SPECS = (
    {
        "subject_label": "Herb",
        "relationship": "TREATS_DISEASE",
        "object_label": "Disease",
        "count": 8,
        "question": "{subject}能治疗哪些疾病？",
    },
    {
        "subject_label": "Formula",
        "relationship": "TREATS_DISEASE",
        "object_label": "Disease",
        "count": 7,
        "question": "{subject}能治疗哪些疾病？",
    },
    {
        "subject_label": "Herb",
        "relationship": "ALLEVIATES_SYMPTOM",
        "object_label": "Symptom",
        "count": 7,
        "question": "{subject}能缓解哪些症状？",
    },
    {
        "subject_label": "Formula",
        "relationship": "ALLEVIATES_SYMPTOM",
        "object_label": "Symptom",
        "count": 6,
        "question": "{subject}能缓解哪些症状？",
    },
    {
        "subject_label": "Formula",
        "relationship": "HAS_INGREDIENT",
        "object_label": "Herb",
        "count": 7,
        "question": "{subject}由哪些药材组成？",
    },
)

AGGREGATE_SPECS = (
    {
        "subject_label": "Formula",
        "relationship": "HAS_INGREDIENT",
        "object_label": "Herb",
        "count": 4,
        "question": "{anchor}由多少味药材组成？",
    },
    {
        "subject_label": "Formula",
        "relationship": "HAS_EFFECT",
        "object_label": "Effect",
        "count": 2,
        "question": "{anchor}关联多少种功效？",
    },
    {
        "subject_label": "Formula",
        "relationship": "TREATS_DISEASE",
        "object_label": "Disease",
        "count": 2,
        "question": "{anchor}能治疗多少种疾病？",
    },
    {
        "subject_label": "Formula",
        "relationship": "ALLEVIATES_SYMPTOM",
        "object_label": "Symptom",
        "count": 2,
        "question": "{anchor}能缓解多少种症状？",
    },
    {
        "subject_label": "Herb",
        "relationship": "TREATS_DISEASE",
        "object_label": "Disease",
        "count": 2,
        "question": "{anchor}能治疗多少种疾病？",
    },
    {
        "subject_label": "Herb",
        "relationship": "ALLEVIATES_SYMPTOM",
        "object_label": "Symptom",
        "count": 2,
        "question": "{anchor}能缓解多少种症状？",
    },
)

UNKNOWN_ENTITY_NAMES = (
    "玄霜参",
    "赤霞草",
    "青霄叶",
    "紫电花",
    "云隐藤",
    "月华根",
    "星沉果",
    "龙涎子",
    "冰蚕草",
    "雪魄花",
    "金乌叶",
    "玉衡参",
    "天河草",
    "玄冰藤",
    "赤霄花",
    "素心根",
    "白曜果",
    "青冥子",
    "九曜叶",
    "紫微参",
)


def load_jsonl(path: Path) -> List[dict]:
    cases = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                cases.append(json.loads(line))
    return cases


def load_base_cases(path: Path) -> List[dict]:
    by_id = {item["id"]: item for item in load_jsonl(path)}
    missing = [case_id for case_id in BASE_CASE_IDS if case_id not in by_id]
    if missing:
        raise RuntimeError("产品集缺失基础用例: " + "、".join(missing))
    return [by_id[case_id] for case_id in BASE_CASE_IDS]


def display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        return "、".join(
            part
            for part in (display_value(item) for item in value)
            if part
        )
    if isinstance(value, dict):
        return "；".join(
            f"{key}: {display_value(item)}"
            for key, item in value.items()
            if display_value(item)
        )
    return str(value).strip()


def value_contains(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:120]


class CaseBuilder:
    def __init__(self, base_cases: Sequence[dict]):
        self.cases = [dict(item) for item in base_cases]
        self.ids = {item["id"] for item in self.cases}
        self.questions = {item["question"] for item in self.cases}

    def add(self, case: dict) -> bool:
        if case["id"] in self.ids or case["question"] in self.questions:
            return False
        self.ids.add(case["id"])
        self.questions.add(case["question"])
        self.cases.append(case)
        return True

    def count_category(self, category: str) -> int:
        return sum(item["category"] == category for item in self.cases)

    def count_categories(self, categories: Iterable[str]) -> int:
        expected = set(categories)
        return sum(item["category"] in expected for item in self.cases)


def fetch(
    session: Any,
    query: str,
    parameters: Dict[str, Any] | None = None,
) -> List[dict]:
    return [
        record.data()
        for record in session.run(query, parameters or {})
    ]


def attribute_rows(
    session: Any,
    label: str,
    attribute: str,
) -> List[dict]:
    query = f"""
MATCH (node:{label})
MATCH (node)-[:FROM_SOURCE]->(source:Source)
WHERE node.{attribute} IS NOT NULL
  AND trim(toString(node.{attribute})) <> ''
RETURN node.name AS name,
       node.{attribute} AS value,
       collect(DISTINCT source.name) AS sources
ORDER BY name
"""
    with_sources = fetch(session, query)
    query_without = f"""
MATCH (node:{label})
WHERE node.{attribute} IS NOT NULL
  AND trim(toString(node.{attribute})) <> ''
  AND NOT EXISTS {{
    MATCH (node)-[:FROM_SOURCE]->(:Source)
  }}
RETURN node.name AS name,
       node.{attribute} AS value,
       [] AS sources
ORDER BY name
"""
    return with_sources + fetch(session, query_without)


def build_attribute_case(
    case_id: str,
    label: str,
    attribute: str,
    category: str,
    question_template: str,
    row: dict,
) -> dict:
    value = display_value(row["value"])
    sources = [
        item.strip()
        for item in row.get("sources", [])
        if isinstance(item, str) and item.strip()
    ]
    return {
        "id": case_id,
        "category": category,
        "question": question_template.format(name=row["name"]),
        "expected_intent": True,
        "expected_entities": [{"label": label, "name": row["name"]}],
        "expected_patterns": [],
        "expected_facts": [
            {
                "subject_label": label,
                "subject_name": row["name"],
                "attribute": attribute,
                "value_contains": value_contains(value),
            }
        ],
        "expected_match_path": "exact",
        "retrieval_probe": row["name"],
        "expected_outcome": "grounded_answer",
        "expected_source_status": "present" if sources else "missing",
        "notes": "属性值由当前 Neo4j 节点直接读取",
    }


def build_source_missing_cases(
    session: Any,
    builder: CaseBuilder,
) -> None:
    buckets = []
    for label, attribute, _category, question in ATTRIBUTE_SPECS:
        rows = [
            row
            for row in attribute_rows(session, label, attribute)
            if not row.get("sources")
        ]
        buckets.append((label, attribute, question, rows))

    cursor = 0
    while builder.count_category("source_missing") < 10:
        progressed = False
        for label, attribute, question, rows in buckets:
            if cursor >= len(rows):
                continue
            row = rows[cursor]
            case = build_attribute_case(
                f"source-missing-{builder.count_category('source_missing') + 1:03d}",
                label,
                attribute,
                "source_missing",
                question,
                row,
            )
            case["expected_source_status"] = "missing"
            if builder.add(case):
                progressed = True
                if builder.count_category("source_missing") >= 10:
                    break
        if not progressed:
            break
        cursor += 1

    if builder.count_category("source_missing") != 10:
        raise RuntimeError("无法从当前图谱构造 10 条无来源属性用例")


def build_attribute_cases(session: Any, builder: CaseBuilder) -> None:
    cases_by_spec = []
    for label, attribute, category, question in ATTRIBUTE_SPECS:
        rows = attribute_rows(session, label, attribute)
        cases_by_spec.append((label, attribute, category, question, rows))

    cursor = 0
    while builder.count_categories({"herb_attribute", "formula_attribute"}) < 45:
        progressed = False
        for label, attribute, category, question, rows in cases_by_spec:
            if cursor >= len(rows):
                continue
            row = rows[cursor]
            case = build_attribute_case(
                f"attribute-generated-{builder.count_categories({'herb_attribute', 'formula_attribute'}) + 1:03d}",
                label,
                attribute,
                category,
                question,
                row,
            )
            if builder.add(case):
                progressed = True
                if builder.count_categories(
                    {"herb_attribute", "formula_attribute"}
                ) >= 45:
                    break
        if not progressed:
            break
        cursor += 1

    if builder.count_categories(
        {"herb_attribute", "formula_attribute"}
    ) != 45:
        raise RuntimeError("无法从当前图谱构造 45 条属性和无来源用例")


def relation_rows(
    session: Any,
    spec: dict,
) -> List[dict]:
    query = f"""
MATCH (subject:{spec['subject_label']})
      -[:{spec['relationship']}]->
      (object:{spec['object_label']})
RETURN DISTINCT subject.name AS subject, object.name AS object
ORDER BY subject, object
"""
    return fetch(session, query)


def build_relation_cases(session: Any, builder: CaseBuilder) -> None:
    created = 0
    for spec in RELATION_FORWARD_SPECS:
        rows = relation_rows(session, spec)
        seen_subjects = set()
        spec_created = 0
        for row in rows:
            if row["subject"] in seen_subjects:
                continue
            seen_subjects.add(row["subject"])
            case_id = f"relation-generated-{created + 1:03d}"
            case = {
                "id": case_id,
                "category": "relation_forward",
                "question": spec["question"].format(
                    subject=row["subject"]
                ),
                "expected_intent": True,
                "expected_entities": [
                    {
                        "label": spec["subject_label"],
                        "name": row["subject"],
                    }
                ],
                "expected_patterns": [
                    {
                        "subject_label": spec["subject_label"],
                        "relationship": spec["relationship"],
                        "object_label": spec["object_label"],
                    }
                ],
                "expected_match_path": "exact",
                "retrieval_probe": row["subject"],
                "expected_outcome": "grounded_answer",
                "notes": "关系方向和对象由当前图谱直接读取",
            }
            if builder.add(case):
                created += 1
                spec_created += 1
            if spec_created >= spec["count"]:
                break

    reverse_specs = (
        (
            "Formula",
            "HAS_INGREDIENT",
            "Herb",
            "哪些方剂含有{object}？",
        ),
        (
            "Herb",
            "HAS_EFFECT",
            "Effect",
            "哪些药材具有{object}功效？",
        ),
        (
            "Formula",
            "TREATS_DISEASE",
            "Disease",
            "哪些方剂能治疗{object}？",
        ),
        (
            "Herb",
            "ALLEVIATES_SYMPTOM",
            "Symptom",
            "哪些药材能缓解{object}？",
        ),
        (
            "Disease",
            "HAS_SYMPTOM",
            "Symptom",
            "哪些疾病会出现{object}？",
        ),
    )
    for subject_label, relationship, object_label, question in reverse_specs:
        rows = relation_rows(
            session,
            {
                "subject_label": subject_label,
                "relationship": relationship,
                "object_label": object_label,
            },
        )
        seen_objects = set()
        reverse_created = 0
        for row in rows:
            if row["object"] in seen_objects:
                continue
            seen_objects.add(row["object"])
            case = {
                "id": f"relation-generated-{created + 1:03d}",
                "category": "relation_reverse",
                "question": question.format(object=row["object"]),
                "expected_intent": True,
                "expected_entities": [
                    {
                        "label": object_label,
                        "name": row["object"],
                    }
                ],
                "expected_patterns": [
                    {
                        "subject_label": subject_label,
                        "relationship": relationship,
                        "object_label": object_label,
                    }
                ],
                "expected_match_path": "exact",
                "retrieval_probe": row["object"],
                "expected_outcome": "grounded_answer",
                "notes": "反向关系由当前图谱直接读取",
            }
            if builder.add(case):
                created += 1
                reverse_created += 1
            if reverse_created >= 2:
                break

    if created != 45:
        raise RuntimeError(f"关系用例生成数量不足：{created}")


def build_aggregate_cases(session: Any, builder: CaseBuilder) -> None:
    created = 0
    for spec in AGGREGATE_SPECS:
        rows = fetch(
            session,
            f"""
MATCH (subject:{spec['subject_label']})
      -[:{spec['relationship']}]->
      (object:{spec['object_label']})
WITH subject, count(DISTINCT object) AS count
WHERE count > 0
RETURN subject.name AS anchor, count
ORDER BY anchor
LIMIT 20
""",
        )
        spec_created = 0
        for row in rows:
            case = {
                "id": f"aggregate-generated-{created + 1:03d}",
                "category": "aggregate_count",
                "question": spec["question"].format(
                    anchor=row["anchor"]
                ),
                "expected_intent": True,
                "expected_entities": [
                    {
                        "label": spec["subject_label"],
                        "name": row["anchor"],
                    }
                ],
                "expected_patterns": [],
                "expected_aggregates": [
                    {
                        "anchor_label": spec["subject_label"],
                        "relationship": spec["relationship"],
                        "result_label": spec["object_label"],
                        "count": int(row["count"]),
                    }
                ],
                "expected_match_path": "exact",
                "retrieval_probe": row["anchor"],
                "expected_outcome": "grounded_answer",
                "notes": "计数由当前图谱只读查询直接计算",
            }
            if builder.add(case):
                created += 1
                spec_created += 1
            if spec_created >= spec["count"]:
                break

    generated_count = builder.count_category("aggregate_count")
    if generated_count != 20:
        raise RuntimeError(f"聚合用例生成数量错误：{generated_count}")


def build_multi_condition_cases(
    session: Any,
    builder: CaseBuilder,
) -> None:
    herbs = fetch(
        session,
        """
MATCH (herb:Herb)<-[:HAS_INGREDIENT]-(formula:Formula)
WITH herb, count(DISTINCT formula) AS formula_count
WHERE formula_count >= 2
RETURN herb.name AS name
ORDER BY formula_count DESC, name
LIMIT 30
""",
    )
    names = [row["name"] for row in herbs]
    generated = 0
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            rows = fetch(
                session,
                """
MATCH (formula:Formula)
WHERE EXISTS {
  MATCH (formula)-[:HAS_INGREDIENT]->(:Herb {name: $first})
}
AND EXISTS {
  MATCH (formula)-[:HAS_INGREDIENT]->(:Herb {name: $second})
}
RETURN formula.name AS formula
ORDER BY formula
LIMIT 1
""",
                {"first": first, "second": second},
            )
            if not rows:
                continue
            case = {
                "id": f"multi-condition-generated-{generated + 1:03d}",
                "category": "multi_condition_list",
                "question": f"同时含有{first}和{second}的方剂有哪些？",
                "expected_intent": True,
                "expected_entities": [
                    {"label": "Herb", "name": first},
                    {"label": "Herb", "name": second},
                ],
                "expected_patterns": [
                    {
                        "subject_label": "Formula",
                        "relationship": "HAS_INGREDIENT",
                        "object_label": "Herb",
                    }
                ],
                "expected_answer_contains": [rows[0]["formula"]],
                "expected_evidence_objects": [first, second],
                "expected_match_path": "exact",
                "retrieval_probe": first,
                "expected_outcome": "grounded_answer",
                "notes": "方剂结果按名称排序并限制在最大返回数量",
            }
            if builder.add(case):
                generated += 1
            if builder.count_category("multi_condition_list") >= 10:
                break
        if builder.count_category("multi_condition_list") >= 10:
            break
    if builder.count_category("multi_condition_list") != 10:
        raise RuntimeError("无法构造 10 条多条件列表用例")


def build_multi_hop_cases(session: Any, builder: CaseBuilder) -> None:
    specs = (
        (
            "HAS_EFFECT",
            "Effect",
            "所含药材有什么功效？",
            3,
        ),
        (
            "TREATS_DISEASE",
            "Disease",
            "所含药材能治疗哪些疾病？",
            3,
        ),
        (
            "ALLEVIATES_SYMPTOM",
            "Symptom",
            "所含药材能缓解哪些症状？",
            2,
        ),
    )
    created = 0
    for second_relationship, target_label, question, count in specs:
        rows = fetch(
            session,
            f"""
MATCH (formula:Formula)-[:HAS_INGREDIENT]->(herb:Herb)
      -[:{second_relationship}]->(target:{target_label})
RETURN DISTINCT formula.name AS formula,
       herb.name AS intermediate,
       target.name AS target
ORDER BY formula, intermediate, target
LIMIT 50
""",
        )
        seen = set()
        spec_created = 0
        for row in rows:
            if row["formula"] in seen:
                continue
            seen.add(row["formula"])
            case = {
                "id": f"multi-hop-generated-{created + 1:03d}",
                "category": "multi_hop",
                "question": f"{row['formula']}{question}",
                "expected_intent": True,
                "expected_entities": [
                    {"label": "Formula", "name": row["formula"]}
                ],
                "expected_patterns": [],
                "expected_answer_contains": [
                    row["intermediate"],
                    row["target"],
                ],
                "expected_paths": [
                    {
                        "root_label": "Formula",
                        "root_name": row["formula"],
                        "first_relationship": "HAS_INGREDIENT",
                        "intermediate_label": "Herb",
                        "second_relationship": second_relationship,
                        "target_label": target_label,
                    }
                ],
                "expected_match_path": "exact",
                "retrieval_probe": row["formula"],
                "expected_outcome": "grounded_answer",
                "notes": "两跳路径由当前图谱只读查询直接读取",
            }
            if builder.add(case):
                created += 1
                spec_created += 1
            if spec_created >= count:
                break

    generated_count = builder.count_category("multi_hop")
    if generated_count != 10:
        raise RuntimeError(f"两跳用例生成数量错误：{generated_count}")


def build_semantic_cases(
    qa_cases: Sequence[dict],
    builder: CaseBuilder,
) -> None:
    source_cases = [
        item
        for item in qa_cases
        if item.get("expected_match_path") == "semantic"
    ]
    if len(source_cases) < 20:
        raise RuntimeError("现有语义正例不足 20 条")
    created = 0
    for index in range(25):
        source = source_cases[index % len(source_cases)]
        prefix = "请问，" if index < 20 else "想请教一下，"
        case = {
            **source,
            "id": f"semantic-product-{index + 1:03d}",
            "category": "semantic_synonym",
            "question": f"{prefix}{source['question']}",
            "expected_outcome": "grounded_answer",
            "notes": "独立措辞的同义召回正例，目标来自当前图谱",
        }
        case.pop("expected_source_status", None)
        if builder.add(case):
            created += 1
    if created != 25:
        raise RuntimeError(f"语义用例生成数量不足：{created}")


def build_unknown_entity_cases(
    session: Any,
    builder: CaseBuilder,
) -> None:
    existing = fetch(
        session,
        """
MATCH (node)
WHERE node.name IN $names
RETURN node.name AS name
""",
        {"names": list(UNKNOWN_ENTITY_NAMES)},
    )
    existing_names = {row["name"] for row in existing}
    if existing_names:
        raise RuntimeError(
            "未知实体名称与当前图谱冲突: "
            + "、".join(sorted(existing_names))
        )
    created = 0
    for name in UNKNOWN_ENTITY_NAMES:
        case = {
            "id": f"unknown-entity-{created + 1:03d}",
            "category": "unknown_entity",
            "question": f"{name}有什么功效？",
            "expected_intent": True,
            "expected_entities": [],
            "expected_patterns": [],
            "expected_match_path": None,
            "retrieval_probe": name,
            "expected_outcome": "clarification",
            "notes": "虚构药材名称，必须请求澄清且不得链接到其他实体",
        }
        if builder.add(case):
            created += 1
    if created != 20:
        raise RuntimeError(f"未知实体用例生成数量不足：{created}")


def missing_property_names(
    session: Any,
    label: str,
    attribute: str,
    limit: int,
) -> List[str]:
    rows = fetch(
        session,
        f"""
MATCH (node:{label})
WHERE (
  node.{attribute} IS NULL
  OR trim(toString(node.{attribute})) = ''
)
AND NOT EXISTS {{
     MATCH (other)
     WHERE other <> node
       AND (
         other.name = node.name
         OR toString(other.alias) CONTAINS node.name
       )
   }}
RETURN node.name AS name
ORDER BY name
LIMIT $limit
""",
        {"limit": limit},
    )
    return [row["name"] for row in rows]


def no_source_names(
    session: Any,
    label: str,
    limit: int,
) -> List[str]:
    rows = fetch(
        session,
        f"""
MATCH (node:{label})
WHERE NOT EXISTS {{
  MATCH (node)-[:FROM_SOURCE]->(:Source)
}}
AND NOT EXISTS {{
  MATCH (other)
  WHERE other <> node
    AND (
      other.name = node.name
      OR toString(other.alias) CONTAINS node.name
    )
}}
RETURN node.name AS name
ORDER BY name
LIMIT $limit
""",
        {"limit": limit},
    )
    return [row["name"] for row in rows]


def build_no_evidence_cases(session: Any, builder: CaseBuilder) -> None:
    specifications = (
        (
            "Herb",
            missing_property_names(session, "Herb", "taboo", 8),
            "{name}有哪些禁忌？",
        ),
        (
            "Formula",
            missing_property_names(session, "Formula", "usage", 5),
            "{name}怎么用？",
        ),
        (
            "Formula",
            missing_property_names(session, "Formula", "taboo", 4),
            "{name}有哪些禁忌？",
        ),
    )
    added = 0
    for label, names, question in specifications:
        for name in names:
            if builder.count_category("no_evidence") >= 10:
                break
            case = {
                "id": f"no-evidence-{added + 1:03d}",
                "category": "no_evidence",
                "question": question.format(name=name),
                "expected_intent": True,
                "expected_entities": [{"label": label, "name": name}],
                "expected_patterns": [],
                "expected_match_path": "exact",
                "retrieval_probe": name,
                "expected_outcome": "no_evidence",
                "notes": "实体存在，但当前图谱缺少所问属性",
            }
            if builder.add(case):
                added += 1

    for label, names in (
        ("Herb", no_source_names(session, "Herb", 5)),
        ("Formula", no_source_names(session, "Formula", 5)),
    ):
        for name in names:
            if builder.count_category("no_evidence") >= 15:
                break
            case = {
                "id": f"no-evidence-{added + 1:03d}",
                "category": "no_evidence",
                "question": f"{name}出自哪些文献？",
                "expected_intent": True,
                "expected_entities": [{"label": label, "name": name}],
                "expected_patterns": [
                    {
                        "subject_label": label,
                        "relationship": "FROM_SOURCE",
                        "object_label": "Source",
                    }
                ],
                "expected_match_path": "exact",
                "retrieval_probe": name,
                "expected_outcome": "no_evidence",
                "notes": "实体存在，但当前图谱没有来源关系",
            }
            if builder.add(case):
                added += 1
    if builder.count_category("no_evidence") != 15:
        raise RuntimeError(
            f"无依据用例生成数量错误：{builder.count_category('no_evidence')}"
        )


def validate_cases(cases: Sequence[dict]) -> None:
    ids = [item["id"] for item in cases]
    questions = [item["question"] for item in cases]
    if len(cases) != TARGET_CASE_COUNT:
        raise RuntimeError(f"产品集数量错误：{len(cases)}")
    if len(ids) != len(set(ids)):
        raise RuntimeError("产品集存在重复 id")
    if len(questions) != len(set(questions)):
        raise RuntimeError("产品集存在重复问题")
    counts = {}
    for item in cases:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    expected = {
        "herb_attribute": 32,
        "formula_attribute": 13,
        "relation_forward": 35,
        "relation_reverse": 10,
        "aggregate_count": 20,
        "multi_condition_list": 10,
        "multi_hop": 10,
        "semantic_synonym": 25,
        "unknown_entity": 20,
        "no_evidence": 15,
        "source_missing": 10,
    }
    if counts != expected:
        raise RuntimeError(
            "产品集分类数量不符: "
            + json.dumps(counts, ensure_ascii=False, sort_keys=True)
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从当前只读 Neo4j 图谱生成 200 条产品固定集"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--base", type=Path, default=BASE_CASES_PATH)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    uri = os.getenv("NEO4J_URI")
    user = os.getenv("NEO4J_USER")
    password = os.getenv("NEO4J_PASSWORD")
    if not all((uri, user, password)):
        raise RuntimeError("缺少 Neo4j 配置")

    builder = CaseBuilder(load_base_cases(args.base))
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            build_source_missing_cases(session, builder)
            build_attribute_cases(session, builder)
            build_relation_cases(session, builder)
            build_aggregate_cases(session, builder)
            build_multi_condition_cases(session, builder)
            build_multi_hop_cases(session, builder)
            build_unknown_entity_cases(session, builder)
            build_no_evidence_cases(session, builder)
    finally:
        driver.close()

    build_semantic_cases(load_jsonl(QA_CASES_PATH), builder)
    validate_cases(builder.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(
            json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for item in builder.cases
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"产品集已生成：{args.output}，共 {len(builder.cases)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
